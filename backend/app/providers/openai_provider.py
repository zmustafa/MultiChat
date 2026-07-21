"""OpenAI-compatible provider adapter (ported/adapted from aznetagent).

Uses the official ``openai`` SDK (``AsyncOpenAI`` / ``AsyncAzureOpenAI``) for OpenAI,
Azure OpenAI, generic OpenAI-compatible gateways (OpenRouter, Together, Groq, LM Studio,
vLLM…), Ollama, Google Gemini (OpenAI-compat endpoint) and GitHub Copilot. This replaces
the previous hand-rolled httpx SSE parsing with the battle-tested SDK, which handles
streaming, tool-call fragment accumulation and usage for us.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncAzureOpenAI, AsyncOpenAI

from ..config import settings
from .base import LLMProvider, StreamEvent, ToolCallRequest, ToolSpec
from .chatgpt_responses import ChatGPTResponsesProvider

# Google's OpenAI-compatible surface for Gemini.
GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"
COPILOT_BASE = "https://api.githubcopilot.com"

# Models (e.g. Azure gpt-5 / o-series) that reject `max_tokens` and require
# `max_completion_tokens` instead. Learned at runtime so the failed first call is paid
# only once per process, then the correct param is sent up front.
_NEEDS_MAX_COMPLETION_TOKENS: set[str] = set()

# Official OpenAI models/errors that require the Responses API for function tools.
# Keep the runtime cache so newly introduced models only pay for one rejected Chat
# Completions request per process.
_RESPONSES_FOR_TOOLS: set[str] = set()
_RESPONSES_ERROR_MARKERS = (
    "use /v1/responses",
    "unsupported_api_for_model",
    "/chat/completions endpoint",
)

_PROVIDER_NAMES = {
    "openai": "OpenAI",
    "openai_eu": "OpenAI (EU)",
    "azure_openai": "Azure OpenAI",
    "azure_foundry": "Azure Foundry",
    "openai_compatible": "OpenAI-compatible",
    "github_copilot": "GitHub Copilot",
    "gemini": "Google Gemini",
    "ollama": "Ollama",
}


class OpenAIProvider(LLMProvider):
    def __init__(
        self,
        *,
        provider: str,
        api_key: str,
        model: str,
        base_url: str = "",
        api_version: str = "2024-10-21",
        default_headers: dict[str, str] | None = None,
        fallback_models: list[str] | None = None,
    ) -> None:
        self._model = model
        self._provider = provider
        self._fallback_models = fallback_models or []
        self._api_key = api_key
        self._default_headers = default_headers

        # Resolve default base URLs for providers that have one.
        if not base_url:
            if provider == "gemini":
                base_url = GEMINI_OPENAI_BASE
            elif provider == "github_copilot":
                base_url = COPILOT_BASE
            elif provider == "openai_eu":
                base_url = "https://eu.api.openai.com/v1"
            elif provider == "ollama":
                base_url = "http://localhost:11434/v1"

        if provider in ("azure_openai", "azure_foundry"):
            self._client: AsyncOpenAI | AsyncAzureOpenAI = AsyncAzureOpenAI(
                api_key=api_key or "",
                azure_endpoint=base_url,
                api_version=api_version or "2024-10-21",
                default_headers=default_headers,
                timeout=settings.LLM_REQUEST_TIMEOUT,
            )
        elif base_url:
            self._client = AsyncOpenAI(
                api_key=api_key or "not-needed",
                base_url=base_url,
                default_headers=default_headers,
                timeout=settings.LLM_REQUEST_TIMEOUT,
            )
        else:
            self._client = AsyncOpenAI(
                api_key=api_key,
                default_headers=default_headers,
                timeout=settings.LLM_REQUEST_TIMEOUT,
            )
        self._responses_base_url = base_url.rstrip("/") or "https://api.openai.com/v1"

    def _label(self) -> str:
        name = _PROVIDER_NAMES.get(self._provider, self._provider.replace("_", " ").title())
        return f"{name} · {self._model}" if self._model else name

    @staticmethod
    def _to_openai_tools(tools: list[ToolSpec] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        # GPT-5.6 defaults to reasoning_effort=auto, a combination the Chat
        # Completions endpoint rejects when function tools are present. Avoid the
        # known failing request and use the endpoint that supports both features.
        if tools and self._uses_official_openai_api() and (
            self._model.startswith("gpt-5.6") or self._model in _RESPONSES_FOR_TOOLS
        ):
            async for event in self._responses_stream(messages, tools, max_tokens):
                yield event
            return

        tool_fragments: dict[int, dict[str, Any]] = {}
        cap = int(max_tokens) if max_tokens else settings.LLM_MAX_TOKENS
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "tools": self._to_openai_tools(tools),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        cap_param = (
            "max_completion_tokens"
            if self._model in _NEEDS_MAX_COMPLETION_TOKENS
            else "max_tokens"
        )
        kwargs[cap_param] = cap

        yield StreamEvent(type="status", phase="connecting", text=f"Connecting to {self._label()}…")
        try:
            stream = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - retry once on token-param rejection
            msg = str(exc).lower()
            cap_val = kwargs.pop("max_tokens", None)
            cap_val = kwargs.pop("max_completion_tokens", cap_val)
            retry = False
            if cap_val and "max_completion_tokens" in msg:
                _NEEDS_MAX_COMPLETION_TOKENS.add(self._model)
                kwargs["max_completion_tokens"] = cap_val
                retry = True
            elif "stream_options" in msg:
                kwargs.pop("stream_options", None)
                kwargs[cap_param] = cap_val
                retry = True
            else:
                kwargs[cap_param] = cap_val

            if self._should_fallback_to_responses(msg, tools):
                _RESPONSES_FOR_TOOLS.add(self._model)
                async for event in self._responses_stream(messages, tools, max_tokens):
                    yield event
                return
            if not retry:
                raise

            try:
                stream = await self._client.chat.completions.create(**kwargs)
            except Exception as retry_exc:  # noqa: BLE001
                retry_msg = str(retry_exc).lower()
                if self._should_fallback_to_responses(retry_msg, tools):
                    _RESPONSES_FOR_TOOLS.add(self._model)
                    async for event in self._responses_stream(messages, tools, max_tokens):
                        yield event
                    return
                raise
        yield StreamEvent(type="status", phase="request_sent", text="Request sent · awaiting response…")

        prompt_tokens = 0
        completion_tokens = 0
        first_chunk = True

        async for chunk in stream:
            if first_chunk:
                first_chunk = False
                yield StreamEvent(type="status", phase="response", text="Response received · generating…")
            if getattr(chunk, "usage", None):
                prompt_tokens = chunk.usage.prompt_tokens or 0
                completion_tokens = chunk.usage.completion_tokens or 0

            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            if delta and delta.content:
                yield StreamEvent(type="token", text=delta.content)

            if delta and delta.tool_calls:
                for tc in delta.tool_calls:
                    frag = tool_fragments.setdefault(
                        tc.index, {"id": "", "name": "", "args": ""}
                    )
                    if tc.id:
                        frag["id"] = tc.id
                    if tc.function and tc.function.name:
                        frag["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        frag["args"] += tc.function.arguments

        if tool_fragments:
            calls: list[ToolCallRequest] = []
            for frag in tool_fragments.values():
                try:
                    args = json.loads(frag["args"]) if frag["args"] else {}
                except json.JSONDecodeError:
                    args = {}
                calls.append(
                    ToolCallRequest(id=frag["id"], name=frag["name"], arguments=args)
                )
            yield StreamEvent(type="tool_calls", tool_calls=calls)

        yield StreamEvent(
            type="done",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    def _uses_official_openai_api(self) -> bool:
        return self._provider in ("openai", "openai_eu")

    def _should_fallback_to_responses(
        self,
        error_message: str,
        tools: list[ToolSpec] | None,
    ) -> bool:
        return bool(
            tools
            and self._uses_official_openai_api()
            and any(marker in error_message for marker in _RESPONSES_ERROR_MARKERS)
        )

    def _responses_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None,
        max_tokens: int | None,
    ) -> AsyncIterator[StreamEvent]:
        responses = ChatGPTResponsesProvider(
            model=self._model,
            oauth_token=self._api_key,
            base_url=self._responses_base_url,
            fallback_models=self._fallback_models,
            default_headers=self._default_headers,
            chatgpt_mode=False,
        )
        return responses.stream(messages, tools, max_tokens)

    async def list_models(self) -> list[str]:
        try:
            resp = await self._client.models.list()
            ids = [m.id for m in resp.data]
            return ids or self._fallback_models
        except Exception:  # noqa: BLE001
            return self._fallback_models

    async def test(self) -> tuple[bool, str]:
        # Genuinely hit the network — do NOT fall back to the configured model list,
        # otherwise an unreachable endpoint would report a false "OK".
        try:
            resp = await self._client.models.list()
            return True, f"Connection OK ({len(resp.data)} models)"
        except Exception as models_exc:  # noqa: BLE001
            # Some OpenAI-compatible endpoints don't expose /models; fall back to a
            # tiny generation to validate connectivity + credentials.
            try:
                async for ev in self.stream(
                    [{"role": "user", "content": "ping"}], max_tokens=1
                ):
                    if ev.type in ("token", "done"):
                        return True, "Connection OK"
                return True, "Connection OK"
            except Exception as exc:  # noqa: BLE001
                detail = str(exc) or str(models_exc)
                return False, detail
