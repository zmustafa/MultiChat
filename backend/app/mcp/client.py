"""Minimal MCP (Model Context Protocol) client over stdio.

Speaks JSON-RPC 2.0 with newline-delimited messages (the MCP stdio transport) to a
subprocess such as `npx -y @microsoft/workiq mcp`. Supports the handshake plus
`tools/list` and `tools/call`, which is all MultiChat needs to expose a server's tools
to the lane models.

Uses a thread-backed `subprocess.Popen` (not asyncio subprocesses) so it works under
any event loop, including uvicorn's SelectorEventLoop on Windows.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
from typing import Any


class McpError(Exception):
    pass


class McpStdioClient:
    def __init__(self, command: str, args: list[str], env: dict[str, str] | None = None):
        self.command = command
        self.args = args
        self.env = env
        self._proc: subprocess.Popen | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._id_lock = threading.Lock()
        self._stderr: list[str] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    async def start(self, init_timeout: float = 60.0) -> dict:
        """Spawn the server and perform the MCP initialize handshake."""
        self._loop = asyncio.get_event_loop()
        exe = shutil.which(self.command) or self.command
        args = list(self.args)
        # On Windows npx/npm resolve to .cmd shims — run them through the shell.
        if sys.platform == "win32" and (
            exe.lower().endswith((".cmd", ".bat")) or self.command in ("npx", "npm")
        ):
            comspec = os.environ.get("COMSPEC", "cmd.exe")
            args = ["/c", exe, *args]
            exe = comspec
        try:
            # WAM/MSAL (used by Work IQ for Microsoft 365 auth) requires an interactive
            # window handle — GetConsoleWindow() must return a valid HWND or it fails with
            # "ApiContractViolation". A pipe-redirected child spawned by a server has no
            # console, so on Windows give it a real one via CREATE_NEW_CONSOLE; the WAM
            # sign-in dialog can then attach to it.
            creationflags = 0
            if sys.platform == "win32":
                creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            self._proc = subprocess.Popen(
                [exe, *args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                env={**os.environ, **(self.env or {})},
                creationflags=creationflags,
            )
        except FileNotFoundError as exc:
            raise McpError(
                f"Could not launch '{self.command}'. Is it installed and on PATH? ({exc})"
            ) from exc

        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

        result = await self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "multichat", "version": "1.0"},
            },
            timeout=init_timeout,
        )
        await self._notify("notifications/initialized", {})
        return result

    async def list_tools(self, timeout: float = 30.0) -> list[dict]:
        res = await self._request("tools/list", {}, timeout=timeout)
        return res.get("tools", [])

    async def call_tool(self, name: str, arguments: dict, timeout: float = 120.0) -> dict:
        return await self._request(
            "tools/call", {"name": name, "arguments": arguments}, timeout=timeout
        )

    async def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                await asyncio.get_event_loop().run_in_executor(None, lambda: self._wait(5))
            except Exception:  # noqa: BLE001
                try:
                    self._proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        self._proc = None
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(McpError("client stopped"))
        self._pending.clear()

    def stderr_tail(self, n: int = 20) -> str:
        return "\n".join(self._stderr[-n:])

    # ---- internals ----------------------------------------------------------

    def _wait(self, timeout: float) -> None:
        try:
            if self._proc:
                self._proc.wait(timeout=timeout)
        except Exception:  # noqa: BLE001
            if self._proc:
                self._proc.kill()

    async def _request(self, method: str, params: dict, timeout: float) -> dict:
        if not self.running or not self._proc or not self._proc.stdin:
            raise McpError("MCP server is not running")
        with self._id_lock:
            self._next_id += 1
            req_id = self._next_id
        assert self._loop is not None
        fut: asyncio.Future = self._loop.create_future()
        self._pending[req_id] = fut
        msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        await asyncio.get_event_loop().run_in_executor(None, self._write, msg)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(req_id, None)
            raise McpError(f"MCP '{method}' timed out. {self.stderr_tail(5)}") from exc

    async def _notify(self, method: str, params: dict) -> None:
        if not self._proc or not self._proc.stdin:
            return
        await asyncio.get_event_loop().run_in_executor(
            None, self._write, {"jsonrpc": "2.0", "method": method, "params": params}
        )

    def _write(self, msg: dict) -> None:
        if self._proc and self._proc.stdin:
            self._proc.stdin.write((json.dumps(msg) + "\n").encode())
            self._proc.stdin.flush()

    def _resolve(self, req_id: int, result: Any, error: Any) -> None:
        fut = self._pending.pop(req_id, None)
        if not fut or fut.done():
            return
        if error is not None:
            fut.set_exception(McpError(error.get("message") or str(error)))
        else:
            fut.set_result(result or {})

    def _read_stdout(self) -> None:
        if not self._proc or not self._proc.stdout:
            return
        for raw in self._proc.stdout:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # non-JSON noise (npx progress, etc.)
            req_id = msg.get("id")
            if req_id is None:
                continue
            if self._loop:
                self._loop.call_soon_threadsafe(
                    self._resolve, req_id, msg.get("result"), msg.get("error")
                )

    def _read_stderr(self) -> None:
        if not self._proc or not self._proc.stderr:
            return
        for raw in self._proc.stderr:
            self._stderr.append(raw.decode(errors="replace").rstrip())
            if len(self._stderr) > 200:
                self._stderr = self._stderr[-200:]


def tool_result_to_text(result: dict) -> str:
    """Flatten an MCP tools/call result into plain text."""
    if not isinstance(result, dict):
        return str(result)
    parts: list[str] = []
    for item in result.get("content") or []:
        if isinstance(item, dict):
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif "text" in item:
                parts.append(str(item["text"]))
            else:
                parts.append(json.dumps(item))
        else:
            parts.append(str(item))
    text = "\n".join(p for p in parts if p)
    if result.get("isError"):
        return f"[tool error] {text}" if text else "[tool error]"
    if not text and "structuredContent" in result:
        return json.dumps(result["structuredContent"])
    return text
