"""MCP lifecycle regressions with fake clients/writes; no subprocess launches."""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.mcp import client as mcp_client
from app.mcp import workiq as workiq_module


@pytest.mark.parametrize("stage", ["start", "list_tools"])
def test_workiq_cancelled_connect_stops_unpublished_client(monkeypatch, stage):
    async def scenario():
        entered = asyncio.Event()

        async def stalled():
            entered.set()
            await asyncio.Event().wait()

        client = SimpleNamespace(
            start=AsyncMock(return_value={}), list_tools=AsyncMock(return_value=[]),
            stop=AsyncMock(), stderr_tail=lambda _n: "", running=True,
        )
        setattr(client, stage, AsyncMock(side_effect=stalled))
        monkeypatch.setattr(workiq_module, "McpStdioClient", lambda *_args: client)
        manager = workiq_module.WorkIqManager()
        # Reconnecting an enabled manager must not leave a phantom enabled state.
        old_client = SimpleNamespace(stop=AsyncMock())
        manager._client = old_client
        manager.enabled = True
        task = asyncio.create_task(manager.connect())
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        old_client.stop.assert_awaited_once()
        client.stop.assert_awaited_once()
        assert not manager.enabled
        assert not manager.connected
        assert manager._tools == []
        assert manager._name_map == {}
        assert manager.error is None

    asyncio.run(scenario())


def test_mcp_cancelled_write_observes_concurrent_stop_error(monkeypatch):
    async def scenario():
        client = mcp_client.McpStdioClient("node", ["unused"])
        client._loop = asyncio.get_running_loop()
        # Already-exited fake process: stop must not launch or wait on anything real.
        process = SimpleNamespace(poll=lambda: None, stdin=object())
        client._proc = process
        entered = threading.Event()
        release = threading.Event()

        def write(_msg):
            entered.set()
            assert release.wait(5)

        monkeypatch.setattr(client, "_write", write)
        task = asyncio.create_task(client._request("test", {}, timeout=10))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            future = next(iter(client._pending.values()))
            process.poll = lambda: 0
            await client.stop()  # resolves the response future before the write returns
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert client._pending == {}
            # asyncio uses this flag to report "Future exception was never retrieved".
            assert not future._log_traceback
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            if "future" in locals():
                future.exception()

    asyncio.run(scenario())



def test_workiq_ordinary_error_and_best_effort_eula_semantics(monkeypatch):
    async def scenario():
        client = SimpleNamespace(
            start=AsyncMock(side_effect=ValueError("startup failed")),
            list_tools=AsyncMock(return_value=[{"name": "accept_eula"}, {"name": "ask"}]),
            call_tool=AsyncMock(side_effect=ValueError("EULA unavailable")),
            stop=AsyncMock(), stderr_tail=lambda _n: "diagnostic tail", running=True,
        )
        monkeypatch.setattr(workiq_module, "McpStdioClient", lambda *_args: client)
        manager = workiq_module.WorkIqManager()
        with pytest.raises(RuntimeError, match="startup failed\ndiagnostic tail"):
            await manager.connect()
        client.stop.assert_awaited_once()
        client.start.side_effect = None
        manager.eula_accepted = True
        status = await manager.connect()
        assert status["connected"] and status["enabled"]
        assert status["error"] is None
        client.call_tool.assert_awaited_once_with("accept_eula", {"eulaUrl": workiq_module.EULA_URL})
        await manager.disconnect()
        assert not manager.enabled and not manager.connected

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["write_error", "write_cancel", "response_cancel", "timeout", "success"])
def test_mcp_request_always_releases_pending_future(monkeypatch, mode):
    async def scenario():
        client = mcp_client.McpStdioClient("node", ["unused"])
        loop = asyncio.get_running_loop()
        client._loop = loop
        client._proc = SimpleNamespace(poll=lambda: None, stdin=object())
        entered = threading.Event()
        release = threading.Event()
        waiting = asyncio.Event()
        original_wait_for = asyncio.wait_for
        captured = []

        async def observe_wait_for(future, timeout):
            if future in client._pending.values():
                waiting.set()
            return await original_wait_for(future, timeout)

        def write(msg):
            captured.append(client._pending[msg["id"]])
            entered.set()
            if mode == "write_error":
                raise OSError("broken pipe")
            if mode == "write_cancel":
                assert release.wait(5)
            if mode == "success":
                loop.call_soon_threadsafe(client._resolve, msg["id"], {"ok": True}, None)

        monkeypatch.setattr(client, "_write", write)
        monkeypatch.setattr(mcp_client.asyncio, "wait_for", observe_wait_for)
        task = asyncio.create_task(client._request("test", {}, timeout=0 if mode == "timeout" else 10))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            if mode in ("write_cancel", "response_cancel"):
                if mode == "response_cancel":
                    await original_wait_for(waiting.wait(), 1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif mode == "write_error":
                with pytest.raises(OSError, match="broken pipe"):
                    await task
            elif mode == "timeout":
                with pytest.raises(mcp_client.McpError, match="timed out"):
                    await task
            else:
                assert await task == {"ok": True}
            assert client._pending == {}
            assert all(future.done() for future in captured)
            # Late server replies after cancellation/timeout must be harmless.
            client._resolve(1, {"late": True}, None)
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            for future in client._pending.values():
                future.cancel()
            client._pending.clear()

    asyncio.run(scenario())
