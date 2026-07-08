"""Anthropic Claude provider (ported/adapted from aznetagent).

Uses Claude's native Messages API with streaming and NATIVE tool calling. Converts the
OpenAI-style message history (used across all our providers) to/from Anthropic's format.
Supports both ``x-api-key`` (API key) and OAuth Bearer (Pro/Max) auth; the OAuth token is
resolved by the caller and passed in, so this class stays free of storage concerns.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..config import settings
from .base import LLMProvider, StreamEvent, ToolCallRequest, ToolSpec

DEFAULT_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
OAUTH_BETA = "oauth-2025-04-20"
CLAUDE_CODE_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."

CLAUDE_FALLBACK_MODELS = [
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
    "claude-3-7-sonnet-latest",
    "claude-3-5-sonnet-latest",
    "claude-3-5-haiku-latest",
]


def _user_content_to_anthropic(content: Any) -> Any:
    """Convert OpenAI user content (str or multimodal list) to Anthropic blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        blocks: list[dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and item.get("text"):
                blocks.append({"type": "text", "text": item["text"]})
            elif item.get("type") == "image_url":
                url = (item.get("image_url") or {}).get("url", "")
                if url.startswith("data:") and ";base64," in url:
                    header, b64 = url.split(";base64,", 1)
                    media_type = header.split(":", 1)[1] if ":" in header else "image/png"
                    blocks.append(
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": media_type, "data": b64},
                        }
                    )
        return blocks or ""
    return ""


def _to_anthropic(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Convert OpenAI-style messages to (system_text, anthropic_messages)."""
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content")
        if role == "system":
            if isinstance(content, str) and content.strip():
                system_parts.append(content.strip())
            continue
        if role == "assistant" and m.get("tool_calls"):
            blocks: list[dict[str, Any]] = []
            if isinstance(content, str) and content.strip():
                blocks.append({"type": "text", "text": content})
            for c in m["tool_calls"]:
                fn = c.get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args.strip() else {}
                    except json.JSONDecodeError:
                        args = {}
                blocks.append(
                    {"type": "tool_use", "id": c.get("id", ""), "name": fn.get("name", ""),
                     "input": args or {}}
                )
            out.append({"role": "assistant", "content": blocks})
            continue
        if role == "tool":
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": m.get("tool_call_id", ""),
                            "content": content if isinstance(content, str) else json.dumps(content),
                        }
                    ],
                }
            )
            continue
        converted = _user_content_to_anthropic(content)
        if converted:
            out.append({"role": role if role in ("user", "assistant") else "user",
                        "content": converted})
    return "\n\n".join(system_parts), out


def _tools_to_anthropic(tools: list[ToolSpec] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    return [
        {"name": t.name, "description": t.description, "input_schema": t.parameters or {"type": "object"}}
        for t in tools
    ]


class ClaudeProvider(LLMProvider):
    """Streams chat via Anthropic's native Messages API with native tool calling."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str = "",
        base_url: str = "",
        use_oauth: bool = False,
        oauth_token: str = "",
    ) -> None:
        self._model = model or "claude-sonnet-4-6"
        self._api_key = (api_key or "").strip()
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._use_oauth = use_oauth
        self._oauth_token = (oauth_token or "").strip()

    def _auth_headers(self) -> dict[str, str]:
        if self._use_oauth:
            return {
                "authorization": f"Bearer {self._oauth_token}",
                "anthropic-version": ANTHROPIC_VERSION,
                "anthropic-beta": OAUTH_BETA,
                "content-type": "application/json",
                "accept": "text/event-stream",
            }
        return {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
            "accept": "text/event-stream",
        }

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        if not self._use_oauth and not self._api_key:
            raise RuntimeError("Claude API key is not set.")

        system_text, anthropic_msgs = _to_anthropic(messages)
        cap = int(max_tokens) if max_tokens else settings.LLM_MAX_TOKENS
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": min(8000, cap),
            "messages": anthropic_msgs,
            "stream": True,
        }
        if self._use_oauth:
            sys_blocks: list[dict[str, Any]] = [{"type": "text", "text": CLAUDE_CODE_SYSTEM}]
            if system_text:
                sys_blocks.append({"type": "text", "text": system_text})
            payload["system"] = sys_blocks
        elif system_text:
            payload["system"] = system_text
        anth_tools = _tools_to_anthropic(tools)
        if anth_tools:
            payload["tools"] = anth_tools

        headers = self._auth_headers()
        url = f"{self._base_url}/v1/messages"
        blocks: dict[int, dict[str, Any]] = {}
        completion_tokens = 0

        _timeout = httpx.Timeout(settings.LLM_REQUEST_TIMEOUT, connect=15.0)
        yield StreamEvent(type="status", phase="connecting", text=f"Connecting to Claude · {self._model}…")
        async with httpx.AsyncClient(timeout=_timeout) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    raise RuntimeError(f"Claude API error {resp.status_code}: {body[:500]}")
                yield StreamEvent(type="status", phase="request_sent", text="Request sent · awaiting response…")

                first = True
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if not data:
                        continue
                    try:
                        evt = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if first:
                        first = False
                        yield StreamEvent(type="status", phase="response", text="Response received · generating…")
                    etype = evt.get("type")
                    if etype == "content_block_start":
                        idx = evt.get("index", 0)
                        cb = evt.get("content_block", {})
                        blocks[idx] = {
                            "type": cb.get("type"),
                            "id": cb.get("id", ""),
                            "name": cb.get("name", ""),
                            "json": "",
                            "text": "",
                        }
                    elif etype == "content_block_delta":
                        idx = evt.get("index", 0)
                        delta = evt.get("delta", {})
                        blk = blocks.setdefault(idx, {"type": "text", "json": "", "text": ""})
                        if delta.get("type") == "text_delta":
                            text = delta.get("text", "")
                            if text:
                                blk["text"] += text
                                yield StreamEvent(type="token", text=text)
                        elif delta.get("type") == "input_json_delta":
                            blk["json"] += delta.get("partial_json", "")
                    elif etype == "message_delta":
                        usage = evt.get("usage", {})
                        completion_tokens = usage.get("output_tokens", completion_tokens)
                    elif etype == "error":
                        msg = (evt.get("error") or {}).get("message", "unknown")
                        raise RuntimeError(f"Claude error: {msg}")

        calls: list[ToolCallRequest] = []
        for _idx, blk in sorted(blocks.items()):
            if blk.get("type") == "tool_use":
                try:
                    args = json.loads(blk["json"]) if blk["json"].strip() else {}
                except json.JSONDecodeError:
                    args = {}
                calls.append(
                    ToolCallRequest(id=blk.get("id", ""), name=blk.get("name", ""), arguments=args)
                )
        if calls:
            yield StreamEvent(type="tool_calls", tool_calls=calls)

        yield StreamEvent(type="done", completion_tokens=max(1, completion_tokens))

    async def list_models(self) -> list[str]:
        headers = self._auth_headers()
        headers.pop("accept", None)
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(f"{self._base_url}/v1/models", headers=headers)
                if resp.status_code >= 400:
                    return list(CLAUDE_FALLBACK_MODELS)
                return [m["id"] for m in resp.json().get("data", [])] or list(CLAUDE_FALLBACK_MODELS)
        except Exception:  # noqa: BLE001
            return list(CLAUDE_FALLBACK_MODELS)

    async def test(self) -> tuple[bool, str]:
        try:
            async for ev in self.stream([{"role": "user", "content": "ping"}], max_tokens=1):
                if ev.type in ("token", "done"):
                    return True, "Connection OK"
            return True, "Connection OK"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
