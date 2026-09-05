"""Per-run SSE replay hub.

Buffers every event of an in-flight broadcast turn and fans it out to live subscribers, so
a client that navigated away and came back can re-attach: it replays what was already
emitted, then tails the rest token-by-token. The buffer is mirrored to disk so a run can
still be replayed after a backend restart.

Token chunks are coalesced in the buffer (never on the wire) — a long answer emits tens of
thousands of one-token frames and the client concatenates deltas anyway.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from collections.abc import AsyncIterator
from weakref import WeakValueDictionary

from .config import settings

logger = logging.getLogger(__name__)


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# Per-run event hub: buffers every SSE event of an in-flight broadcast turn and fans it
# out to live subscribers. Lets a client that navigated away and returned re-attach to the
# live stream — replaying what was already emitted, then tailing the rest token-by-token —
# instead of only polling partial text. Keyed by (session_id, turn_id). Kept a short while
# after the run finishes so a returning client can still catch the tail + terminal "done".
HUB_TTL_SECONDS = 45

# The hub's event buffer is also mirrored (batched) to a small NDJSON file under this dir, so
# a client can still replay a run whose in-memory hub was lost to a backend --reload/restart.
_RUNS_DIR = os.path.join(settings.UPLOAD_DIR, "runs")
_RUN_FILE_MAX_AGE = 3600  # startup sweep deletes run buffers older than this (crash leftovers)

_hubs: dict[tuple[str, str], _RunHub] = {}


class _RunFileState:
    """One writer lock across generations of the same canonical replay path."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.generation = object()


# Constructors are loop-owned. Workers retain their hub/state, so an old in-flight
# writer shares the new generation's lock even after it leaves _hubs. Weak values avoid
# retaining one lock per historical turn forever. Ownership is process-local, like _hubs.
_run_files: WeakValueDictionary[str, _RunFileState] = WeakValueDictionary()


# Run-buffer file names are built from ids that ultimately come from a request, so anything
# outside this character set is replaced before the name is used as a path component.
_RUN_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def _run_file_path(session_id: str, turn_id: str) -> str:
    """Absolute path of a run's NDJSON buffer, constrained to the runs directory."""
    safe = _RUN_NAME_UNSAFE.sub("_", f"{session_id}__{turn_id}")[:200]
    root = os.path.realpath(_RUNS_DIR)
    path = os.path.realpath(os.path.join(root, f"{safe}.ndjson"))
    if not path.startswith(root + os.sep):
        raise ValueError("invalid run buffer path")
    return path


def _delete_run_file(hub: _RunHub) -> None:
    """Off-loop cleanup: retire queued writers and delete only this generation's file."""
    hub._retired = True
    with hub._flush_lock:
        if hub._file_state.generation is not hub._generation:
            return
        try:
            os.remove(hub._path)
        except OSError:
            pass


def _sweep_stale_run_files() -> None:
    """Remove run buffers left behind by crashes/old restarts (older than the max age).
    Recent files are kept so an in-progress run interrupted by a --reload can still resume."""
    try:
        now = time.time()
        for name in os.listdir(_RUNS_DIR):
            path = os.path.join(_RUNS_DIR, name)
            try:
                if now - os.path.getmtime(path) > _RUN_FILE_MAX_AGE:
                    os.remove(path)
            except OSError:
                pass
    except OSError:
        pass


class _ChunkRun:
    """An open run of consecutive ``chunk`` events for one lane.

    Token deltas are appended as plain strings and joined into a single SSE frame only
    when the run is flushed or replayed, so buffering stays O(1) per token instead of
    re-serialising a growing JSON payload on every one.
    """

    __slots__ = ("lane_id", "parts")

    def __init__(self, lane_id: str, delta: str) -> None:
        self.lane_id = lane_id
        self.parts: list[str] = [delta]

    def render(self) -> str:
        return sse("chunk", {"lane_id": self.lane_id, "delta": "".join(self.parts)})


def _render_event(item: str | _ChunkRun) -> str:
    return item if isinstance(item, str) else item.render()


def _parse_chunk(item: str) -> tuple[str, str] | None:
    if not item.startswith("event: chunk\n"):
        return None
    try:
        payload = json.loads(item.split("data: ", 1)[1])
        return payload["lane_id"], payload["delta"]
    except (IndexError, KeyError, ValueError):
        return None


class _RunHub:
    """In-memory buffer + pub/sub for one broadcast turn's SSE events, mirrored to disk."""

    def __init__(self, session_id: str, turn_id: str) -> None:
        # Buffering and subscriptions are loop-owned; only mirror snapshots run off-loop.
        # Entries are rendered SSE frames, except for still-open chunk runs.
        self.events: list[str | _ChunkRun] = []
        self.subscribers: set[asyncio.Queue] = set()
        self.done = False
        self._path = _run_file_path(session_id, turn_id)
        self._flushed = 0  # events already written to disk
        self._last_flush = 0.0
        # Index of the open chunk run per lane. A long answer emits tens of thousands of
        # one-token frames; the client concatenates deltas anyway, so keeping them
        # separate only cost memory, disk writes and replay time.
        self._open_runs: dict[str, int] = {}
        # Never hold the buffer lock during rendering or file I/O. The separate writer
        # lock serializes snapshots, including a to_thread flush whose awaiter cancelled.
        self._buffer_lock = threading.Lock()
        state = _run_files.get(self._path)
        if state is None:
            state = _RunFileState()
            _run_files[self._path] = state
        self._file_state = state
        self._generation = object()
        # Claiming ownership must not wait on an old worker's disk I/O. A worker already
        # holding the shared lock may finish, but the replacement resets its file AFTER
        # that write; subsequent old writes/deletes cannot touch the replacement's data.
        state.generation = self._generation
        self._flush_lock = state.lock
        self._file_initialized = False
        self._retired = False

    def _buffer(self, item: str) -> None:
        chunk = _parse_chunk(item)
        with self._buffer_lock:
            if chunk is None:
                self._open_runs.clear()  # a non-chunk event ends every open run
                self.events.append(item)
                return
            lane_id, delta = chunk
            idx = self._open_runs.get(lane_id)
            # Snapshotting seals open runs before rendering, so no writer can observe
            # parts changing and no later token can extend an in-flight disk snapshot.
            if idx is not None and idx >= self._flushed:
                run = self.events[idx]
                if isinstance(run, _ChunkRun):
                    run.parts.append(delta)
                    return
            self._open_runs[lane_id] = len(self.events)
            self.events.append(_ChunkRun(lane_id, delta))

    async def put(self, item: str) -> None:
        # Duck-types asyncio.Queue.put so run_lane can publish through it unchanged.
        # Live subscribers always get the original frame; only the replay buffer is
        # coalesced, so streaming stays token-by-token.
        for q in list(self.subscribers):
            q.put_nowait(item)
        self._buffer(item)
        # Batched, off-thread mirror to disk (throttled so token chunks don't hammer I/O).
        if len(self.events) - self._flushed >= 25 or (
            time.time() - self._last_flush > 1.5
        ):
            await asyncio.to_thread(self._flush_sync)

    def _flush_sync(self) -> None:
        with self._flush_lock:
            if self._retired or self._file_state.generation is not self._generation:
                return
            with self._buffer_lock:
                end = len(self.events)
                new = self.events[self._flushed : end]
                if not new and self._file_initialized:
                    return
                self._open_runs.clear()  # freeze the snapshot, not future chunk runs
            start = None
            try:
                os.makedirs(_RUNS_DIR, exist_ok=True)
                # Keep the legacy filename/NDJSON format for restart replay, but reset
                # it on this generation's first successful open, never on each batch.
                # Unbuffered binary I/O makes offsets exact and prevents close() from
                # flushing a failed batch back out after rollback.
                with open(self._path, "a+b", buffering=0) as f:
                    if not self._file_initialized:
                        f.seek(0)
                        f.truncate()
                        self._file_initialized = True
                    f.seek(0, os.SEEK_END)
                    start = f.tell()
                    for e in new:
                        data = (json.dumps(_render_event(e)) + "\n").encode("utf-8")
                        if f.write(data) != len(data):
                            raise OSError("short replay mirror write")
                with self._buffer_lock:
                    self._flushed = end  # never mark concurrently appended events as saved
                    self._last_flush = time.time()
            except OSError:
                if start is not None:
                    try:
                        # Reopen even on close errors. The shared lock prevents another
                        # generation from writing between the failed batch and rollback.
                        with open(self._path, "r+b", buffering=0) as f:
                            f.seek(start)
                            f.truncate()
                    except OSError:
                        # Unknown suffix: never append/retry over it. Rebuild from the
                        # intact in-memory buffer if the filesystem becomes usable again.
                        self._file_initialized = False
                        with self._buffer_lock:
                            self._flushed = 0
                        logger.warning("Replay mirror rollback failed; next flush will rebuild it")
                # Leave the snapshot pending so a later flush can retry it.

    async def finish(self) -> None:
        self.done = True
        # A previous cancelled put may still be writing. Wait off-loop, not on its lock.
        try:
            await asyncio.to_thread(self._flush_sync)
        finally:
            for q in list(self.subscribers):
                q.put_nowait(None)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        for e in self.events:  # replay everything emitted so far
            q.put_nowait(_render_event(e))
        if self.done:
            q.put_nowait(None)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)


def has_hub(session_id: str, turn_id: str) -> bool:
    return (session_id, turn_id) in _hubs


def _read_run_lines(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return f.readlines()


async def resume_stream(session_id: str, turn_id: str) -> AsyncIterator[str]:
    """Re-attach to an in-flight broadcast: replay buffered events, then tail live ones."""
    hub = _hubs.get((session_id, turn_id))
    if hub is not None:
        q = hub.subscribe()
        try:
            while True:
                item = await q.get()
                if item is None:
                    break
                yield item
        finally:
            hub.unsubscribe(q)
        return
    # No live hub (most likely the backend restarted mid-run) — replay the persisted buffer
    # if one exists. The run itself is gone, so this yields the captured partial then closes;
    # the client's reconcile/poll + the persisted DB message take over from there.
    try:
        path = _run_file_path(session_id, turn_id)
    except ValueError:
        return
    if not os.path.exists(path):
        return
    try:
        lines = await asyncio.to_thread(_read_run_lines, path)
    except OSError:
        return
    invalid = 0
    try:
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except ValueError:
                invalid += 1
                continue
            if not isinstance(frame, str):
                invalid += 1
                continue
            yield frame
    finally:
        if invalid:
            # Never log record contents, parser exceptions, or user-controlled paths.
            logger.warning("Skipped %d invalid replay record(s)", invalid)

