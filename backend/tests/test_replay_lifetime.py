"""Replay generations and failed writes, using fake timers and temporary files only."""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import broadcast, run_hub


@pytest.fixture
def replay_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(run_hub, "_RUNS_DIR", str(tmp_path))
    hubs = {}
    monkeypatch.setattr(run_hub, "_hubs", hubs)
    monkeypatch.setattr(broadcast, "_hubs", hubs)
    monkeypatch.setattr(broadcast, "_detached_tasks", set())
    return tmp_path


def _disk(hub):
    return [json.loads(line) for line in run_hub._read_run_lines(hub._path)]


async def _collect(stream):
    return [frame async for frame in stream]


def test_old_ttl_cannot_remove_replacement_hub_or_replay(replay_dir, monkeypatch):
    async def scenario():
        loop = asyncio.get_running_loop()
        call_later = loop.call_later
        timers = []
        release = asyncio.Event()

        def fake_timer(delay, callback, *args, **kwargs):
            if delay == broadcast.HUB_TTL_SECONDS:
                timers.append((callback, args))
                return SimpleNamespace(cancel=lambda: None)
            return call_later(delay, callback, *args, **kwargs)

        async def fire(timer):
            callback, args = timer
            result = callback(*args)
            if inspect.isawaitable(result):
                await result

        async def lane(_sid, lane_id, _tid, _msg, hub, _shared):
            await hub.put(run_hub.sse("chunk", {"lane_id": lane_id, "delta": lane_id}))
            if lane_id == "new":
                await release.wait()

        monkeypatch.setattr(loop, "call_later", fake_timer)
        monkeypatch.setattr(broadcast, "run_lane", lane)
        await _collect(broadcast.multiplex("s", "t", [("old", {})]))
        old_timer = timers.pop()
        stream = broadcast.multiplex("s", "t", [("new", {})])
        try:
            frame = await anext(stream)
            replacement = run_hub._hubs[("s", "t")]
            await asyncio.to_thread(replacement._flush_sync)
            await fire(old_timer)
            assert run_hub._hubs.get(("s", "t")) is replacement
            assert _disk(replacement) == [frame]
            resumed = asyncio.create_task(_collect(run_hub.resume_stream("s", "t")))
            release.set()
            tail = await _collect(stream)
            assert await resumed == [frame, *tail]
            # The replacement's own TTL still works, and repeated old callbacks are safe.
            await fire(old_timer)
            assert run_hub.has_hub("s", "t")
            await fire(timers.pop())
            assert not run_hub.has_hub("s", "t")
            assert not Path(replacement._path).exists()
        finally:
            release.set()
            await _collect(stream)
            await stream.aclose()

    asyncio.run(scenario())


def test_overload_cleanup_after_finish_preserves_replacement(replay_dir, monkeypatch):
    monkeypatch.setattr(broadcast, "MAX_DETACHED_TASKS", 0)

    async def lane(_sid, _lid, _tid, _msg, hub, _shared):
        await hub.put(run_hub.sse("status", {"text": "old"}))
        await asyncio.Event().wait()

    monkeypatch.setattr(broadcast, "run_lane", lane)

    async def scenario():
        stream = broadcast.multiplex("s", "t", [("old", {})])
        await anext(stream)
        old = run_hub._hubs[("s", "t")]
        finish = old.finish
        entered = asyncio.Event()
        release = asyncio.Event()

        async def gated_finish():
            await finish()
            entered.set()
            await release.wait()

        monkeypatch.setattr(old, "finish", gated_finish)
        closing = asyncio.create_task(stream.aclose())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            replacement = run_hub._RunHub("s", "t")
            run_hub._hubs[("s", "t")] = replacement
            frame = run_hub.sse("status", {"text": "new"})
            await replacement.put(frame)
        finally:
            release.set()
            await closing
        assert run_hub._hubs.get(("s", "t")) is replacement
        assert _disk(replacement) == [frame]

    asyncio.run(scenario())


def test_new_generation_resets_legacy_file_once_and_resumes_after_restart(replay_dir):
    path = Path(run_hub._run_file_path("s", "t"))
    old = [run_hub.sse("chunk", {"lane_id": "a", "delta": "old"}), run_hub.sse("done", {})]
    path.write_text("".join(json.dumps(frame) + "\n" for frame in old), encoding="utf-8")
    # Existing pre-generation files remain readable when no new run has started.
    assert asyncio.run(_collect(run_hub.resume_stream("s", "t"))) == old
    hub = run_hub._RunHub("s", "t")
    frames = [run_hub.sse("status", {"text": "new"}), run_hub.sse("done", {})]
    for frame in frames:
        hub._buffer(frame)
        hub._flush_sync()
    assert _disk(hub) == frames
    # No registry entry: the same disk-only path used after a process restart.
    assert asyncio.run(_collect(run_hub.resume_stream("s", "t"))) == frames


def test_replacement_serializes_with_stalled_old_snapshot(replay_dir, monkeypatch):
    old = run_hub._RunHub("s", "t")
    old_frame = run_hub.sse("status", {"text": "old"})
    old._buffer(old_frame)
    entered = threading.Event()
    attempted = threading.Event()
    release = threading.Event()
    render = run_hub._render_event

    def gated_render(item):
        if item == old_frame:
            entered.set()
            assert release.wait(5)
        return render(item)

    class ObservedLock:
        def __init__(self, lock):
            self.lock = lock

        def __enter__(self):
            attempted.set()
            self.lock.acquire()

        def __exit__(self, *_exc):
            self.lock.release()

    monkeypatch.setattr(run_hub, "_render_event", gated_render)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(old._flush_sync)
        try:
            assert entered.wait(5)
            replacement = run_hub._RunHub("s", "t")
            replacement._flush_lock = ObservedLock(replacement._flush_lock)
            frame = run_hub.sse("status", {"text": "new"})
            replacement._buffer(frame)
            new_writer = pool.submit(replacement._flush_sync)
            assert attempted.wait(5)
        finally:
            release.set()
        writer.result(timeout=5)
        new_writer.result(timeout=5)
    old._buffer(run_hub.sse("done", {"text": "stale"}))
    old._flush_sync()
    assert _disk(replacement) == [frame]


def test_delayed_cleanup_worker_rechecks_generation(replay_dir):
    old = run_hub._RunHub("s", "t")
    old._buffer(run_hub.sse("status", {"text": "old"}))
    old._flush_sync()
    entered = threading.Event()
    release = threading.Event()
    lock = old._flush_lock

    class GatedLock:
        def __enter__(self):
            entered.set()
            assert release.wait(5)
            lock.acquire()

        def __exit__(self, *_exc):
            lock.release()

    old._flush_lock = GatedLock()
    with ThreadPoolExecutor(max_workers=1) as pool:
        cleanup = pool.submit(run_hub._delete_run_file, old)
        try:
            assert entered.wait(5)
            replacement = run_hub._RunHub("s", "t")
            frame = run_hub.sse("status", {"text": "new"})
            replacement._buffer(frame)
            replacement._flush_sync()
        finally:
            release.set()
        cleanup.result(timeout=5)
    assert _disk(replacement) == [frame]


def test_cleanup_waits_for_writer_and_prevents_late_file_resurrection(replay_dir, monkeypatch):
    hub = run_hub._RunHub("s", "t")
    hub._buffer(run_hub.sse("status", {"text": "pending"}))
    entered = threading.Event()
    deleting = threading.Event()
    release = threading.Event()
    lock = hub._flush_lock
    render = run_hub._render_event

    class ObservedLock:
        def __enter__(self):
            if entered.is_set():
                deleting.set()
            lock.acquire()

        def __exit__(self, *_exc):
            lock.release()

    def gated_render(item):
        entered.set()
        assert release.wait(5)
        return render(item)

    hub._flush_lock = ObservedLock()
    monkeypatch.setattr(run_hub, "_render_event", gated_render)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(hub._flush_sync)
        try:
            assert entered.wait(5)
            cleanup = pool.submit(run_hub._delete_run_file, hub)
            assert deleting.wait(5)
        finally:
            release.set()
        writer.result(timeout=5)
        cleanup.result(timeout=5)
    assert not Path(hub._path).exists()
    hub._buffer(run_hub.sse("done", {}))
    hub._flush_sync()
    assert not Path(hub._path).exists()


def test_empty_generation_finish_clears_old_mirror(replay_dir):
    path = Path(run_hub._run_file_path("s", "t"))
    path.write_text(json.dumps(run_hub.sse("done", {})) + "\n", encoding="utf-8")
    hub = run_hub._RunHub("s", "t")
    asyncio.run(hub.finish())
    assert _disk(hub) == []


@pytest.mark.parametrize("failure", ["write", "short_write", "close"])
@pytest.mark.parametrize("append", [False, True])
@pytest.mark.parametrize("rollback_fails", [False, True])
def test_failed_snapshot_rolls_back_before_retry(
    replay_dir, monkeypatch, failure, append, rollback_fails,
):
    hub = run_hub._RunHub("s", "t")
    frames = []
    if append:
        frames.append(run_hub.sse("status", {"text": "already saved"}))
        hub._buffer(frames[0])
        hub._flush_sync()
    before = Path(hub._path).read_bytes() if append else b""
    flushed = hub._flushed
    for text in ["first pending", "second pending"]:
        frames.append(run_hub.sse("status", {"text": text}))
        hub._buffer(frames[-1])
    real_open = open
    opened = False

    class FaultyFile:
        def __init__(self, file):
            self.file = file
            self.writes = 0

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self.file.close()
            if failure == "close":
                raise OSError("injected close failure")

        def __getattr__(self, name):
            return getattr(self.file, name)

        def write(self, data):
            self.writes += 1
            if self.writes == 2 and failure != "close":
                count = self.file.write(data[:len(data) // 2])
                self.file.flush()
                if failure == "short_write":
                    return count
                raise OSError("injected partial write")
            return self.file.write(data)

    def faulty_open(*args, **kwargs):
        nonlocal opened
        if opened and rollback_fails:
            raise OSError("injected rollback failure")
        file = real_open(*args, **kwargs)
        if not opened:
            opened = True
            return FaultyFile(file)
        return file

    with monkeypatch.context() as patch:
        patch.setattr(run_hub, "open", faulty_open, raising=False)
        hub._flush_sync()
    if rollback_fails:
        assert hub._flushed == 0
        assert not hub._file_initialized  # retry must rebuild, not append to a bad suffix
    else:
        assert hub._flushed == flushed
        assert Path(hub._path).read_bytes() == before
    frames.append(run_hub.sse("done", {}))
    hub._buffer(frames[-1])
    hub._flush_sync()
    assert _disk(hub) == frames
    assert hub._flushed == len(hub.events)


def test_disk_resume_warns_without_payloads_and_yields_only_strings(replay_dir, caplog):
    secret = "private-payload-must-not-be-logged"
    first = run_hub.sse("status", {"text": "ready"})
    last = run_hub.sse("done", {})
    records = [json.dumps(first), '{"broken": "' + secret]
    records += [json.dumps(value) for value in [{"secret": secret}, [secret], 3, 1.5, True, None]]
    records += ["", json.dumps(last)]
    Path(run_hub._run_file_path("s", "t")).write_text("\n".join(records), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="app.run_hub"):
        replayed = asyncio.run(_collect(run_hub.resume_stream("s", "t")))
    assert replayed == [first, last]
    assert caplog.records
    assert secret not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
