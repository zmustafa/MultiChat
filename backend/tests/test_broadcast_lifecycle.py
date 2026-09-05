"""Isolated broadcast lifecycle/cache regressions: no DB or provider calls."""
from __future__ import annotations

import asyncio
import base64
import os
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app import broadcast, run_hub


@pytest.fixture
def image_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(broadcast.settings, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(broadcast, "_image_cache", OrderedDict())
    monkeypatch.setattr(broadcast, "_image_cache_bytes", 0)
    return tmp_path


def _attachment(name="image.png", mime="image/png"):
    return SimpleNamespace(kind="image", storage_path=name, mime_type=mime)


def _url_size(part):
    return len(part["image_url"]["url"].encode("utf-8"))


def test_image_cache_byte_accounting_and_eviction(image_cache, monkeypatch):
    (image_cache / "a.png").write_bytes(b"aaa")
    (image_cache / "b.png").write_bytes(b"bbb")
    first = broadcast._image_part(None, _attachment("a.png"))
    assert broadcast._image_cache_bytes == _url_size(first)
    monkeypatch.setattr(broadcast, "_IMAGE_CACHE_MAX_BYTES", _url_size(first))
    second = broadcast._image_part(None, _attachment("b.png"))
    assert list(broadcast._image_cache.values()) == [(second, _url_size(second))]
    assert broadcast._image_cache_bytes == _url_size(second)


def test_oversized_image_is_returned_but_not_cached(image_cache, monkeypatch):
    (image_cache / "image.png").write_bytes(b"image")
    monkeypatch.setattr(broadcast, "_IMAGE_CACHE_MAX_BYTES", 1)
    assert broadcast._image_part(None, _attachment()) is not None
    assert not broadcast._image_cache
    assert broadcast._image_cache_bytes == 0


def test_image_cache_invalidates_same_second_same_size_update(image_cache):
    path = image_cache / "image.png"
    path.write_bytes(b"old")
    stamp = 1_700_000_000_100_000_000
    os.utime(path, ns=(stamp, stamp))
    first = broadcast._image_part(None, _attachment())
    path.write_bytes(b"new")
    os.utime(path, ns=(stamp + 100_000_000, stamp + 100_000_000))
    second = broadcast._image_part(None, _attachment())
    assert first != second
    assert second["image_url"]["url"].endswith(base64.b64encode(b"new").decode())


def test_image_cache_key_includes_mime_and_canonical_root(image_cache, monkeypatch):
    path = image_cache / "image.png"
    path.write_bytes(b"old")
    first = broadcast._image_part(None, _attachment())
    other_mime = broadcast._image_part(None, _attachment(mime="image/jpeg"))
    assert other_mime["image_url"]["url"].startswith("data:image/jpeg;")
    alias = broadcast._image_part(None, _attachment("./image.png"))
    assert alias is first
    other_root = image_cache / "other"
    other_root.mkdir()
    other_path = other_root / "image.png"
    other_path.write_bytes(b"new")
    stamp = path.stat().st_mtime_ns
    os.utime(other_path, ns=(stamp, stamp))
    monkeypatch.setattr(broadcast.settings, "UPLOAD_DIR", str(other_root))
    assert broadcast._image_part(None, _attachment()) != first


def test_concurrent_same_image_is_counted_once(image_cache, monkeypatch):
    (image_cache / "image.png").write_bytes(b"image")
    barrier = threading.Barrier(2)
    encode = base64.b64encode

    def simultaneous_encode(data):
        # Both threads must finish file I/O before either may insert. Also proves
        # expensive work is not done while holding the cache lock.
        barrier.wait(timeout=5)
        return encode(data)

    monkeypatch.setattr(broadcast.base64, "b64encode", simultaneous_encode)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(broadcast._image_part, None, _attachment()) for _ in range(2)]
        parts = [future.result(timeout=5) for future in futures]
    assert parts[0] is parts[1]
    assert len(broadcast._image_cache) == 1
    assert broadcast._image_cache_bytes == _url_size(parts[0])


@pytest.mark.parametrize("mode", ["outer_cancel", "consumer_close", "stop", "error", "exhaust"])
def test_events_until_cancel_closes_source_and_joins_tasks(mode):
    async def scenario():
        entered = asyncio.Event()
        closed = asyncio.Event()
        cancel = asyncio.Event()

        async def source():
            try:
                if mode == "consumer_close":
                    yield "first"
                if mode == "error":
                    raise ValueError("source failed")
                if mode == "exhaust":
                    return
                entered.set()
                await asyncio.Event().wait()
                yield "unreachable"
            finally:
                closed.set()

        before = asyncio.all_tasks()
        underlying = source()
        stream = broadcast._events_until_cancel(underlying, cancel)
        read = asyncio.create_task(anext(stream))
        try:
            if mode == "consumer_close":
                assert await read == "first"
                await stream.aclose()
            elif mode == "error":
                with pytest.raises(ValueError, match="source failed"):
                    await read
            elif mode == "exhaust":
                with pytest.raises(StopAsyncIteration):
                    await read
            else:
                await asyncio.wait_for(entered.wait(), 1)
                if mode == "outer_cancel":
                    read.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await read
                else:
                    cancel.set()
                    with pytest.raises(StopAsyncIteration):
                        await read
            assert closed.is_set()
            assert not (asyncio.all_tasks() - before), "helper tasks must be awaited, not orphaned"
        finally:
            leftover = asyncio.all_tasks() - before
            for task in leftover:
                task.cancel()
            await asyncio.gather(*leftover, return_exceptions=True)
            await stream.aclose()
            await underlying.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("capacity,detached", [(3, False), (4, True)])
def test_detach_capacity_counts_incoming_lanes_and_watcher(tmp_path, monkeypatch, capacity, detached):
    monkeypatch.setattr(run_hub, "_RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(broadcast, "_hubs", {})
    monkeypatch.setattr(broadcast, "_detached_tasks", set())
    monkeypatch.setattr(broadcast, "MAX_DETACHED_TASKS", capacity)

    async def fake_lane(_sid, lane_id, _tid, _msg, queue, _shared):
        await queue.put(run_hub.sse("status", {"lane_id": lane_id}))
        await asyncio.Event().wait()

    monkeypatch.setattr(broadcast, "run_lane", fake_lane)

    async def scenario():
        before = asyncio.all_tasks()
        existing = asyncio.create_task(asyncio.Event().wait())
        broadcast._detached_tasks.add(existing)
        stream = broadcast.multiplex("s", "t", [("a", {}), ("b", {})])
        try:
            await asyncio.wait_for(anext(stream), 2)
            await stream.aclose()
            assert len(broadcast._detached_tasks) == (4 if detached else 1)
            assert len(broadcast._detached_tasks) <= capacity
            assert (("s", "t") in broadcast._hubs) is detached
        finally:
            remaining = asyncio.all_tasks() - before
            for task in remaining:
                task.cancel()
            await asyncio.gather(*remaining, return_exceptions=True)
            await stream.aclose()

    asyncio.run(scenario())
