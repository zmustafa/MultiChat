"""The SSE replay hub coalesces token chunks in its buffer. Live subscribers must still
receive every original frame, and a replay must reconstruct byte-identical text."""
from __future__ import annotations

import asyncio
import json

import pytest

from app import run_hub


def _drain(queue: asyncio.Queue) -> list[str]:
    items: list[str] = []
    while not queue.empty():
        item = queue.get_nowait()
        if item is None:
            break
        items.append(item)
    return items


def _text_of(frames: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for frame in frames:
        if not frame.startswith("event: chunk\n"):
            continue
        payload = json.loads(frame.split("data: ", 1)[1])
        out[payload["lane_id"]] = out.get(payload["lane_id"], "") + payload["delta"]
    return out


@pytest.mark.parametrize("lanes", [1, 3])
def test_replay_reconstructs_the_same_text(tmp_path, monkeypatch, lanes):
    monkeypatch.setattr(run_hub, "_RUNS_DIR", str(tmp_path))

    async def scenario():
        hub = run_hub._RunHub("s1", "t1")
        expected = {f"lane{i}": "" for i in range(lanes)}
        for step in range(200):
            for i in range(lanes):
                lane = f"lane{i}"
                delta = f"tok{step}-"
                expected[lane] += delta
                await hub.put(run_hub.sse("chunk", {"lane_id": lane, "delta": delta}))
        await hub.put(run_hub.sse("done", {"turn_id": "t1"}))
        return hub, expected

    hub, expected = asyncio.run(scenario())
    replayed = _drain(hub.subscribe())
    assert _text_of(replayed) == expected
    # The whole point: the buffer must be far smaller than the frame count.
    assert len(hub.events) < 200 * lanes


def test_live_subscribers_still_get_every_frame(tmp_path, monkeypatch):
    monkeypatch.setattr(run_hub, "_RUNS_DIR", str(tmp_path))

    async def scenario():
        hub = run_hub._RunHub("s2", "t2")
        queue = hub.subscribe()
        for step in range(30):
            await hub.put(
                run_hub.sse("chunk", {"lane_id": "a", "delta": str(step)})
            )
        return queue

    queue = asyncio.run(scenario())
    frames = _drain(queue)
    # Streaming stays token-by-token for the attached client.
    assert len(frames) == 30


def test_non_chunk_events_keep_their_order(tmp_path, monkeypatch):
    monkeypatch.setattr(run_hub, "_RUNS_DIR", str(tmp_path))

    async def scenario():
        hub = run_hub._RunHub("s3", "t3")
        await hub.put(run_hub.sse("chunk", {"lane_id": "a", "delta": "one "}))
        await hub.put(run_hub.sse("tool_call", {"lane_id": "a", "tool": "web_search"}))
        await hub.put(run_hub.sse("chunk", {"lane_id": "a", "delta": "two"}))
        return hub

    hub = asyncio.run(scenario())
    kinds = [f.split("\n", 1)[0] for f in _drain(hub.subscribe())]
    assert kinds == ["event: chunk", "event: tool_call", "event: chunk"]


def test_disk_mirror_matches_the_buffer(tmp_path, monkeypatch):
    monkeypatch.setattr(run_hub, "_RUNS_DIR", str(tmp_path))

    async def scenario():
        hub = run_hub._RunHub("s4", "t4")
        for step in range(120):
            await hub.put(
                run_hub.sse("chunk", {"lane_id": "a", "delta": f"{step},"})
            )
        await hub.put(run_hub.sse("done", {"turn_id": "t4"}))
        await hub.finish()
        return hub

    hub = asyncio.run(scenario())
    lines = run_hub._read_run_lines(run_hub._run_file_path("s4", "t4"))
    from_disk = [json.loads(line) for line in lines if line.strip()]
    assert _text_of(from_disk) == _text_of([run_hub._render_event(e) for e in hub.events])
