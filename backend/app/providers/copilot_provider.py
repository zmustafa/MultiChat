"""GitHub Copilot provider (ported concept from aznetagent).

Copilot's OpenAI-compatible ``/chat/completions`` endpoint serves most models (Claude,
Gemini, GPT-4.x), but the GPT-5 / o-series reasoning models are NOT accessible there —
Copilot returns ``unsupported_api_for_model``. Those models must use the OpenAI
**Responses** API (``{base}/responses``). This adapter tries chat/completions first and
transparently falls back to the Responses API on that error.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from .base import LLMProvider, StreamEvent, ToolSpec
from .chatgpt_responses import ChatGPTResponsesProvider
from .openai_provider import OpenAIProvider

_RESPONSES_MARKERS = ("unsupported_api_for_model", "/chat/completions endpoint")
_VISION_MARKERS = ("image media type not supported", "not supported for vision")


def _has_image(messages: list[dict[str, Any]]) -> bool:
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "image_url":
                    return True
    return False


class CopilotProvider(LLMProvider):
    def __init__(
        self,
        *,
        token: str,
        model: str,
        base_url: str,
        editor_headers: dict[str, str],
        fallback_models: list[str] | None = None,
    ) -> None:
        self._token = token
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._editor_headers = editor_headers
        self._fallback_models = fallback_models or []
        self._chat = OpenAIProvider(
            provider="github_copilot",
            api_key=token,
            model=model,
            base_url=self._base_url,
            default_headers=editor_headers,
            fallback_models=fallback_models,
        )

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        # Vision on Copilot: models differ in how they accept images. Claude/Gemini
        # accept them on /chat/completions when the Copilot-Vision-Request header is
        # set; GPT-5.x need the Responses API. So for image turns we try chat/completions
        # (with the vision header) first and fall back to Responses on rejection.
        has_image = _has_image(messages)
        if has_image:
            chat = OpenAIProvider(
                provider="github_copilot",
                api_key=self._token,
                model=self._model,
                base_url=self._base_url,
                default_headers={**self._editor_headers, "Copilot-Vision-Request": "true"},
                fallback_models=self._fallback_models,
            )
            markers = _RESPONSES_MARKERS + _VISION_MARKERS
        else:
            chat = self._chat
            markers = _RESPONSES_MARKERS

        emitted = False
        try:
            async for ev in chat.stream(messages, tools, max_tokens):
                if ev.type in ("token", "tool_calls"):
                    emitted = True
                yield ev
            return
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            # Only fall back if nothing was streamed yet and the error is a known
            # "wrong endpoint for this model" signal.
            if emitted or not any(m in msg for m in markers):
                raise

        # GPT-5 / o-series (and vision-capable reasoning models) use the Responses API.
        async for ev in self._responses_stream(messages, tools, max_tokens):
            yield ev

    def _responses_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None,
        max_tokens: int | None,
    ) -> AsyncIterator[StreamEvent]:
        responses = ChatGPTResponsesProvider(
            model=self._model,
            oauth_token=self._token,
            base_url=self._base_url,
            default_headers=self._editor_headers,
            chatgpt_mode=False,
            fallback_models=self._fallback_models,
        )
        return responses.stream(messages, tools, max_tokens)

    async def list_models(self) -> list[str]:
        return await self._chat.list_models()

    async def test(self) -> tuple[bool, str]:
        return await self._chat.test()
