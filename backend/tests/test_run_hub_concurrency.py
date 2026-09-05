"""Deterministic replay mirror races; only temporary files are used."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import run_hub


def _frames(hub):
    return [json.loads(line) for line in run_hub._read_run_lines(hub._path)]


@pytest.mark.parametrize("chunk", [False, True])
def test_put_during_mirror_write_is_not_skipped(tmp_path, monkeypatch, chunk):
    monkeypatch.setattr(run_hub, "_RUNS_DIR", str(tmp_path))
    hub = run_hub._RunHub("session", "turn")
    entered = threading.Event()
    release = threading.Event()
    original_render = run_hub._render_event

    def paused_render(item):
        rendered = original_render(item)
        if not entered.is_set():
            entered.set()
            assert release.wait(5), "test did not release the writer"
        return rendered

    monkeypatch.setattr(run_hub, "_render_event", paused_render)
    first = run_hub.sse("chunk", {"lane_id": "a", "delta": "first"})
    second = (
        run_hub.sse("chunk", {"lane_id": "a", "delta": "second"})
        if chunk else run_hub.sse("done", {"turn_id": "turn"})
    )

    async def scenario():
        hub._buffer(first)
        writer = asyncio.create_task(asyncio.to_thread(hub._flush_sync))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            hub._last_flush = time.time()  # this put must not itself start another flush
            await asyncio.wait_for(hub.put(second), 1)
        finally:
            release.set()
            await writer
        await asyncio.to_thread(hub._flush_sync)

    asyncio.run(scenario())
    assert _frames(hub) == [first, second]
    assert [run_hub._render_event(item) for item in hub.events] == [first, second]
    assert hub._flushed == len(hub.events)


def test_overlapping_flushes_write_each_snapshot_once(tmp_path, monkeypatch):
    monkeypatch.setattr(run_hub, "_RUNS_DIR", str(tmp_path))
    hub = run_hub._RunHub("session", "overlap")
    frame = run_hub.sse("status", {"text": "ready"})
    hub._buffer(frame)
    entered = threading.Event()
    second_attempted = threading.Event()
    release = threading.Event()
    original_render = run_hub._render_event

    def paused_render(item):
        entered.set()
        assert release.wait(5)
        return original_render(item)

    # Signal exactly when the second flush reads the old implementation's slice or
    # attempts the fixed implementation's file lock. No scheduling sleeps needed.
    class ObservedEvents(list):
        def __getitem__(self, key):
            if isinstance(key, slice) and entered.is_set():
                second_attempted.set()
            return super().__getitem__(key)

    class ObservedLock:
        def __init__(self, lock):
            self.lock = lock

        def __enter__(self):
            if entered.is_set():
                second_attempted.set()
            self.lock.acquire()

        def __exit__(self, *_exc):
            self.lock.release()

    hub.events = ObservedEvents(hub.events)
    if hasattr(hub, "_flush_lock"):
        hub._flush_lock = ObservedLock(hub._flush_lock)
    monkeypatch.setattr(run_hub, "_render_event", paused_render)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(hub._flush_sync)
        try:
            assert entered.wait(5)
            second = pool.submit(hub._flush_sync)
            assert second_attempted.wait(5)
        finally:
            release.set()
        first.result(timeout=5)
        second.result(timeout=5)
    assert _frames(hub) == [frame]


def test_failed_mirror_is_retried_without_losing_buffer(tmp_path, monkeypatch):
    monkeypatch.setattr(run_hub, "_RUNS_DIR", str(tmp_path))
    hub = run_hub._RunHub("session", "retry")
    first = run_hub.sse("chunk", {"lane_id": "a", "delta": "first"})
    second = run_hub.sse("chunk", {"lane_id": "a", "delta": "second"})
    hub._buffer(first)

    def fail_open(*_args, **_kwargs):
        raise OSError("temporary mirror failure")

    with monkeypatch.context() as patch:
        patch.setattr(run_hub, "open", fail_open, raising=False)
        hub._flush_sync()
    assert hub._flushed == 0
    hub._buffer(second)
    hub._flush_sync()
    assert _frames(hub) == [run_hub._render_event(item) for item in hub.events]
    assert "".join(json.loads(f.split("data: ", 1)[1])["delta"] for f in _frames(hub)) == "firstsecond"


def test_token_buffer_does_not_render_growing_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(run_hub, "_RUNS_DIR", str(tmp_path))
    hub = run_hub._RunHub("session", "tokens")

    def unexpected_render(*_args):
        pytest.fail("buffering must not render token runs")

    monkeypatch.setattr(run_hub._ChunkRun, "render", unexpected_render)
    for _ in range(10000):
        hub._buffer(run_hub.sse("chunk", {"lane_id": "a", "delta": "x"}))
    assert len(hub.events) == 1
    assert len(hub.events[0].parts) == 10000


@pytest.mark.parametrize("cancel_finish", [False, True])
def test_finish_waits_off_loop_for_cancelled_put_writer(tmp_path, monkeypatch, cancel_finish):
    monkeypatch.setattr(run_hub, "_RUNS_DIR", str(tmp_path))
    hub = run_hub._RunHub("session", "cancelled-writer")
    entered = threading.Event()
    finishing = threading.Event()
    release = threading.Event()
    original_render = run_hub._render_event
    original_lock = hub._flush_lock

    class ObservedLock:
        def __enter__(self):
            if entered.is_set():
                finishing.set()
            original_lock.acquire()

        def __exit__(self, *_exc):
            original_lock.release()

    def paused_render(item):
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        return original_render(item)

    hub._flush_lock = ObservedLock()
    monkeypatch.setattr(run_hub, "_render_event", paused_render)
    first = run_hub.sse("chunk", {"lane_id": "a", "delta": "first"})
    last = run_hub.sse("done", {"turn_id": "cancelled-writer"})

    async def scenario():
        queue = hub.subscribe()
        writer = asyncio.create_task(hub.put(first))
        finish = None
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            writer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await writer
            hub._last_flush = time.time()
            await hub.put(last)
            finish = asyncio.create_task(hub.finish())
            assert await asyncio.to_thread(finishing.wait, 5)
            assert not finish.done()
            # The loop can still run while finish's worker is waiting for the writer.
            assert queue.get_nowait() == first
            assert queue.get_nowait() == last
            if cancel_finish:
                finish.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await finish
        finally:
            release.set()
            await asyncio.gather(writer, return_exceptions=True)
            if finish is not None:
                await asyncio.gather(finish, return_exceptions=True)
            await asyncio.to_thread(hub._flush_sync)  # drain cancelled workers too
        assert queue.get_nowait() is None

    asyncio.run(scenario())
    assert _frames(hub) == [first, last]

