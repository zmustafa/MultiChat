"""Single-user Work IQ integration manager.

Holds one long-lived MCP connection to the Work IQ server (`npx -y @microsoft/workiq
mcp` by default) and exposes its discovered tools to MultiChat. Self-hosted / single
user: the connection is shared by every chat.
"""
from __future__ import annotations

import asyncio
import re

from .client import McpStdioClient, tool_result_to_text

# Default command to launch the Work IQ MCP server.
DEFAULT_COMMAND = "npx"
DEFAULT_ARGS = ["-y", "@microsoft/workiq@latest", "mcp"]

# Terms accepted once via Integration settings; auto-applied on every connection.
EULA_URL = "https://github.com/microsoft/work-iq"

_PREFIX = "workiq_"


def _sanitize(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)


def _is_eula(raw_name: str | None) -> bool:
    return "eula" in (raw_name or "").lower()


class WorkIqManager:
    def __init__(self) -> None:
        self._client: McpStdioClient | None = None
        self._tools: list[dict] = []  # raw MCP tool defs
        self._name_map: dict[str, str] = {}  # exposed name -> mcp name
        self.enabled = False
        self.eula_accepted = False
        self.error: str | None = None
        self.command = DEFAULT_COMMAND
        self.args = list(DEFAULT_ARGS)
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.running

    async def connect(self, command: str | None = None, args: list[str] | None = None) -> dict:
        """Spawn the Work IQ MCP server and discover its tools."""
        async with self._lock:
            if command:
                self.command = command
            if args is not None:
                self.args = args
            await self._stop_locked()
            self.error = None
            client = McpStdioClient(self.command, self.args)
            try:
                await client.start()
                tools = await client.list_tools()
            except Exception as exc:  # noqa: BLE001
                detail = str(exc) or type(exc).__name__
                tail = client.stderr_tail(6)
                self.error = (f"{detail}" + (f"\n{tail}" if tail else "")).strip()
                await client.stop()
                raise RuntimeError(self.error) from exc
            self._client = client
            self._tools = tools
            self._name_map = {}
            for t in tools:
                exposed = _PREFIX + _sanitize(t.get("name", ""))
                self._name_map[exposed] = t["name"]
            self.enabled = True
            # If the EULA was already accepted (once, in Integration settings), replay
            # that acceptance on this fresh MCP session so tools like `ask` never ask
            # for consent again after a reconnect/restart.
            if self.eula_accepted:
                await self._accept_eula_locked()
            return self.status()

    async def disconnect(self) -> None:
        async with self._lock:
            self.enabled = False
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        if self._client:
            try:
                await self._client.stop()
            except Exception:  # noqa: BLE001
                pass
        self._client = None
        self._tools = []
        self._name_map = {}

    async def _accept_eula_locked(self) -> None:
        """Best-effort: send EULA acceptance to the live MCP server (no lock)."""
        if not self._client:
            return
        mcp_name = next((t["name"] for t in self._tools if _is_eula(t.get("name"))), None)
        if not mcp_name:
            return
        try:
            await self._client.call_tool(mcp_name, {"eulaUrl": EULA_URL})
        except Exception:  # noqa: BLE001
            pass

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "connected": self.connected,
            "eula_accepted": self.eula_accepted,
            "command": f"{self.command} {' '.join(self.args)}",
            "error": self.error,
            "tools": [
                {
                    "name": _PREFIX + _sanitize(t.get("name", "")),
                    "raw_name": t.get("name"),
                    "description": (t.get("description") or "")[:200],
                }
                for t in self._tools
            ],
        }

    def exposed_tools(self) -> list[dict]:
        """Discovered tools as MultiChat-facing defs: {name, description, parameters}.

        The EULA/consent tool is intentionally omitted — acceptance is handled once in
        Integration settings and replayed automatically, so the model should never see
        (or prompt the user about) it.
        """
        out = []
        for t in self._tools:
            if _is_eula(t.get("name")):
                continue
            out.append(
                {
                    "name": _PREFIX + _sanitize(t.get("name", "")),
                    "description": t.get("description") or f"Work IQ tool {t.get('name')}",
                    "parameters": t.get("inputSchema")
                    or {"type": "object", "properties": {}},
                }
            )
        return out

    def eula_tool_name(self) -> str | None:
        for t in self._tools:
            if _is_eula(t.get("name")):
                return _PREFIX + _sanitize(t.get("name", ""))
        return None

    async def call(self, exposed_name: str, arguments: dict) -> str:
        mcp_name = self._name_map.get(exposed_name)
        if not mcp_name or not self._client:
            return f"[Work IQ] tool '{exposed_name}' is not available."
        try:
            result = await self._client.call_tool(mcp_name, arguments)
            return tool_result_to_text(result) or "(no content)"
        except Exception as exc:  # noqa: BLE001
            return f"[Work IQ error] {exc}"


# Process-wide singleton (single-user self-hosted).
workiq = WorkIqManager()
