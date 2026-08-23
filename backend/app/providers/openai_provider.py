"""OpenAI-compatible provider adapter (ported/adapted from aznetagent).

Uses the official ``openai`` SDK (``AsyncOpenAI`` / ``AsyncAzureOpenAI``) for OpenAI,
Azure OpenAI, generic OpenAI-compatible gateways (OpenRouter, Together, Groq, LM Studio,
vLLM…), Ollama, Google Gemini (OpenAI-compat endpoint) and GitHub Copilot. This replaces
the previous hand-rolled httpx SSE parsing with the battle-tested SDK, which handles
streaming, tool-call fragment accumulation and usage for us.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections import OrderedDict
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

# Conservative per-family output-token ceilings, used only when the endpoint does not
# advertise a real limit. `settings.LLM_MAX_TOKENS` is a global upper bound; a model that
# cannot emit that many tokens would reject the request, so the cap is clamped first.
_DEFAULT_MAX_OUTPUT = 16384
_MAX_OUTPUT_BY_PREFIX: tuple[tuple[str, int], ...] = (
    ("gpt-3.5", 4096),
    ("gpt-4-", 4096),
    ("gpt-4o", 16384),
    ("gpt-4.1", 32768),
    ("gpt-5", 100000),
    ("claude", 64000),
    ("gemini", 65536),
    ("o1", 100000),
    ("o3", 100000),
    ("o4", 100000),
)
# Ceilings learned at runtime from a rejected request, keyed by lowercased model id.
_LEARNED_MAX_OUTPUT: dict[str, int] = {}
# `capabilities.limits.max_output_tokens` as advertised by an endpoint's /models route
# (GitHub Copilot and several gateways expose it), keyed by endpoint then model id.
_ADVERTISED_MAX_OUTPUT: dict[str, dict[str, int]] = {}


def _static_max_output_tokens(model: str) -> int:
    m = (model or "").lower()
    for prefix, limit in _MAX_OUTPUT_BY_PREFIX:
        if m.startswith(prefix):
            return limit
    return _DEFAULT_MAX_OUTPUT


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


# A fresh AsyncOpenAI carries its own httpx connection pool, so building one per lane per
# turn meant a new TLS handshake for every message (and the pools were never closed).
# Clients are cached by everything that affects the connection; a rotated OAuth bearer
# therefore yields a new entry and the stale one is evicted by the LRU.
_MAX_CACHED_CLIENTS = 32
_client_cache: OrderedDict[str, AsyncOpenAI | AsyncAzureOpenAI] = OrderedDict()
_CREDENTIAL_FINGERPRINT_KEY = secrets.token_bytes(32)


def _credential_fingerprint(credential: str) -> str:
    return hmac.new(
        _CREDENTIAL_FINGERPRINT_KEY,
        credential.encode(),
        hashlib.sha256,
    ).hexdigest()


def _client_key(parts: dict[str, Any]) -> str:
    blob = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _cached_client(key: str, factory) -> AsyncOpenAI | AsyncAzureOpenAI:  # noqa: ANN001
    existing = _client_cache.get(key)
    if existing is not None:
        _client_cache.move_to_end(key)
        return existing
    client = factory()
    _client_cache[key] = client
    while len(_client_cache) > _MAX_CACHED_CLIENTS:
        _, evicted = _client_cache.popitem(last=False)
        _schedule_close(evicted)
    return client


def _schedule_close(client: AsyncOpenAI | AsyncAzureOpenAI) -> None:
    """Close an evicted client without blocking; a stream may still be draining it."""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_safe_close(client))


async def _safe_close(client: AsyncOpenAI | AsyncAzureOpenAI) -> None:
    try:
        await client.close()
    except Exception:  # noqa: BLE001 - eviction must never surface to a request
        pass


async def close_cached_clients() -> None:
    """Close and drop every pooled client (called on application shutdown)."""
    clients = list(_client_cache.values())
    _client_cache.clear()
    for client in clients:
        await _safe_close(client)


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
            key = _client_key(
                {
                    "kind": "azure",
                    "endpoint": base_url,
                    "version": api_version or "2024-10-21",
                    "auth": _credential_fingerprint(api_key or ""),
                    "headers": default_headers or {},
                }
            )
            self._client: AsyncOpenAI | AsyncAzureOpenAI = _cached_client(
                key,
                lambda: AsyncAzureOpenAI(
                    api_key=api_key or "",
                    azure_endpoint=base_url,
                    api_version=api_version or "2024-10-21",
                    default_headers=default_headers,
                    timeout=settings.LLM_REQUEST_TIMEOUT,
                ),
            )
        else:
            key = _client_key(
                {
                    "kind": "openai",
                    "base": base_url,
                    "auth": _credential_fingerprint(api_key or ""),
                    "headers": default_headers or {},
                }
            )
            self._client = _cached_client(
                key,
                lambda: AsyncOpenAI(
                    api_key=api_key or "not-needed",
                    default_headers=default_headers,
                    timeout=settings.LLM_REQUEST_TIMEOUT,
                    **({"base_url": base_url} if base_url else {}),
                ),
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
        cap = max(1, min(await self._model_max_output(), cap))
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
            elif cap_val and cap_val > 4096 and (
                "max_tokens" in msg or "max_output_tokens" in msg
            ):
                # Gateway rejected the requested ceiling; remember a safe value.
                _LEARNED_MAX_OUTPUT[self._model.lower()] = 4096
                kwargs[cap_param] = 4096
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

    async def _model_max_output(self) -> int:
        """Largest output-token cap this model accepts.

        Prefers a ceiling learned from a rejected request, then the one the endpoint
        advertises on /models, and finally the static per-family table.
        """
        model = (self._model or "").lower()
        if model in _LEARNED_MAX_OUTPUT:
            return _LEARNED_MAX_OUTPUT[model]
        advertised = await self._advertised_limits()
        if model in advertised:
            return advertised[model]
        return _static_max_output_tokens(self._model)

    async def _advertised_limits(self) -> dict[str, int]:
        """`capabilities.limits.max_output_tokens` per model, fetched once per endpoint."""
        key = f"{self._provider}|{self._responses_base_url}"
        cached = _ADVERTISED_MAX_OUTPUT.get(key)
        if cached is not None:
            return cached
        limits: dict[str, int] = {}
        try:
            resp = await self._client.models.list()
            for m in resp.data:
                raw = m.model_dump() if hasattr(m, "model_dump") else {}
                value = ((raw.get("capabilities") or {}).get("limits") or {}).get(
                    "max_output_tokens"
                )
                model_id = str(raw.get("id") or "").lower()
                if model_id and isinstance(value, int) and value > 0:
                    limits[model_id] = value
        except Exception:  # noqa: BLE001 - discovery is best effort
            limits = {}
        _ADVERTISED_MAX_OUTPUT[key] = limits
        return limits

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
