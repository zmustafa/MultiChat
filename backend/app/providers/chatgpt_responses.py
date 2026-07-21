"""ChatGPT (OAuth) provider via the Codex Responses API (adapted from aznetagent).

Talks to ``https://chatgpt.com/backend-api/codex/responses`` using the OpenAI *Responses*
API streaming format (NOT chat/completions). Auth is the ChatGPT OAuth access token, which
the caller resolves and passes in (plus the account id). Supports native function tools in
Responses format and normalizes streamed deltas/function calls back to ``StreamEvent``.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..config import settings
from .base import LLMProvider, StreamEvent, ToolCallRequest, ToolSpec

DEFAULT_BASE_URL = "https://chatgpt.com/backend-api/codex"

CHATGPT_FALLBACK_MODELS = ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-4o", "o4-mini"]


def _split_system_and_convo(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    inputs: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content")
        if role == "system":
            if isinstance(content, str) and content.strip():
                system_parts.append(content.strip())
            continue
        if role == "tool":
            inputs.append(
                {
                    "type": "function_call_output",
                    "call_id": m.get("tool_call_id", ""),
                    "output": content if isinstance(content, str) else json.dumps(content),
                }
            )
            continue
        text_type = "output_text" if role == "assistant" else "input_text"
        parts: list[dict[str, Any]] = []
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text" and item.get("text"):
                    parts.append({"type": text_type, "text": item["text"]})
                elif item.get("type") == "image_url":
                    url = (item.get("image_url") or {}).get("url", "")
                    if url:
                        parts.append({"type": "input_image", "image_url": url})
        elif isinstance(content, str) and content.strip():
            parts.append({"type": text_type, "text": content})
        if parts:
            inputs.append({"role": role, "content": parts})
        # An assistant turn that called tools must emit a `function_call` item for each
        # call BEFORE its matching `function_call_output`, or the Responses API rejects
        # the request ("No tool call found for function call output with call_id …").
        if role == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                args = fn.get("arguments")
                if not isinstance(args, str):
                    args = json.dumps(args or {})
                inputs.append(
                    {
                        "type": "function_call",
                        "call_id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "arguments": args,
                    }
                )
    return "\n\n".join(system_parts), inputs


class ChatGPTResponsesProvider(LLMProvider):
    def __init__(
        self,
        *,
        model: str,
        oauth_token: str,
        account_id: str = "",
        base_url: str = "",
        fallback_models: list[str] | None = None,
        default_headers: dict[str, str] | None = None,
        chatgpt_mode: bool = True,
    ) -> None:
        self._model = model or "gpt-5.5"
        self._token = (oauth_token or "").strip()
        self._account_id = (account_id or "").strip()
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._fallback_models = fallback_models or CHATGPT_FALLBACK_MODELS
        self._default_headers = default_headers or {}
        self._chatgpt_mode = chatgpt_mode

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if self._chatgpt_mode:
            headers["OpenAI-Beta"] = "responses=experimental"
            headers["originator"] = "codex_cli_rs"
            headers["session_id"] = str(uuid.uuid4())
            if self._account_id:
                headers["chatgpt-account-id"] = self._account_id
        headers.update(self._default_headers)
        return headers

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        instructions, inputs = _split_system_and_convo(messages)
        payload: dict[str, Any] = {
            "model": self._model,
            "instructions": instructions,
            "input": inputs or [{"role": "user", "content": [{"type": "input_text", "text": ""}]}],
            "stream": True,
            "store": False,
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                }
                for t in tools
            ]
        if max_tokens and not self._chatgpt_mode:
            payload["max_output_tokens"] = int(max_tokens)

        url = f"{self._base_url}/responses"
        provider_name = "ChatGPT" if self._chatgpt_mode else "OpenAI"
        fn_acc: dict[str, dict[str, Any]] = {}
        completed_calls: dict[str, dict[str, Any]] = {}
        current_event: str | None = None
        _timeout = httpx.Timeout(settings.LLM_REQUEST_TIMEOUT, connect=15.0)

        yield StreamEvent(type="status", phase="connecting", text=f"Connecting to {provider_name} · {self._model}…")
        async with httpx.AsyncClient(timeout=_timeout) as client:
            async with client.stream("POST", url, json=payload, headers=self._headers()) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    raise RuntimeError(f"{provider_name} API error {resp.status_code}: {body[:500]}")
                yield StreamEvent(type="status", phase="request_sent", text="Request sent · awaiting response…")
                first = True
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("event:"):
                        current_event = line[len("event:"):].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        evt = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    etype = current_event or (evt.get("type") if isinstance(evt, dict) else "")
                    if etype == "response.output_text.delta":
                        if first:
                            first = False
                            yield StreamEvent(type="status", phase="response", text="Response received · generating…")
                        yield StreamEvent(type="token", text=evt.get("delta", ""))
                    elif etype == "response.output_item.added":
                        item = evt.get("item", {})
                        if item.get("type") == "function_call":
                            fn_acc[item.get("id", "")] = {
                                "call_id": item.get("call_id", ""),
                                "name": item.get("name", ""),
                                "args": item.get("arguments", "") or "",
                            }
                    elif etype == "response.function_call_arguments.delta":
                        entry = fn_acc.get(evt.get("item_id", ""))
                        if entry:
                            entry["args"] += evt.get("delta", "")
                    elif etype == "response.output_item.done":
                        # Authoritative for Copilot: it obfuscates/rotates item_id on
                        # every argument delta, so delta-accumulation never matches.
                        # This event carries the complete arguments + a stable call_id.
                        item = evt.get("item", {})
                        if item.get("type") == "function_call":
                            cid = item.get("call_id") or item.get("id", "")
                            completed_calls[cid] = {
                                "call_id": cid,
                                "name": item.get("name", ""),
                                "args": item.get("arguments", "") or "",
                            }
                    elif etype in ("response.error", "error"):
                        msg = (evt.get("error") or {}).get("message") if isinstance(evt, dict) else None
                        raise RuntimeError(f"{provider_name} error: {msg or data[:200]}")
                    elif etype == "response.completed":
                        break

        if fn_acc or completed_calls:
            calls: list[ToolCallRequest] = []
            seen: set[str] = set()
            # Authoritative complete calls first.
            for entry in completed_calls.values():
                cid = entry["call_id"] or entry["name"]
                seen.add(cid)
                try:
                    args = json.loads(entry["args"]) if entry["args"] else {}
                except json.JSONDecodeError:
                    args = {}
                calls.append(
                    ToolCallRequest(id=cid, name=entry["name"], arguments=args)
                )
            # Delta-accumulated calls (standard OpenAI) — skip empties/duplicates.
            for entry in fn_acc.values():
                cid = entry["call_id"] or entry["name"]
                if cid in seen or not entry["args"]:
                    continue
                seen.add(cid)
                try:
                    args = json.loads(entry["args"]) if entry["args"] else {}
                except json.JSONDecodeError:
                    args = {}
                calls.append(
                    ToolCallRequest(id=cid, name=entry["name"], arguments=args)
                )
            if calls:
                yield StreamEvent(type="tool_calls", tool_calls=calls)

        yield StreamEvent(type="done")

    async def list_models(self) -> list[str]:
        return list(self._fallback_models)

    async def test(self) -> tuple[bool, str]:
        try:
            async for ev in self.stream([{"role": "user", "content": "ping"}], max_tokens=16):
                if ev.type in ("token", "done"):
                    return True, "Connection OK"
            return True, "Connection OK"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
