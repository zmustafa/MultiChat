"""Exercise the installed OpenAI SDK over an in-process HTTPX transport."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import AsyncOpenAI

from app.providers import openai_provider
from app.providers.base import StreamEvent, ToolSpec


@pytest.mark.parametrize("mode", ["request_sent", "token", "cancel", "error", "complete"])
def test_openai_response_is_closed_before_provider_exits(monkeypatch, mode):
    async def scenario():
        waiting = asyncio.Event()

        class Body(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                payload = {
                    "id": "test", "object": "chat.completion.chunk", "created": 0,
                    "model": "test-model",
                    "choices": [{"index": 0, "delta": {"content": "hello"}}],
                }
                yield f"data: {json.dumps(payload)}\n\n".encode()
                if mode == "cancel":
                    waiting.set()
                    await asyncio.Event().wait()
                if mode == "error":
                    raise ValueError("broken body")
                yield b'data: {"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}\n\n'
                yield b"data: [DONE]\n\n"

            async def aclose(self):
                self.closed = True

        body = Body()
        client = AsyncOpenAI(
            api_key="test-only", base_url="https://example.invalid/v1",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, stream=body, headers={"content-type": "text/event-stream"})
            )),
        )
        monkeypatch.setattr(openai_provider, "_cached_client", lambda *_args: client)
        monkeypatch.setattr(openai_provider.OpenAIProvider, "_model_max_output", AsyncMock(return_value=1024))
        provider = openai_provider.OpenAIProvider(
            provider="openai_compatible", api_key="test-only", credential_scope="test",
            model="test-model", base_url="https://example.invalid/v1",
        )
        stream = provider.stream([{"role": "user", "content": "test"}])
        reader = None
        try:
            async for event in stream:
                if mode == "request_sent" and event.phase == "request_sent":
                    break
                if event.type == "token":
                    assert event.text == "hello"
                    break
            if mode in ("request_sent", "token"):
                await stream.aclose()
            elif mode == "cancel":
                reader = asyncio.create_task(anext(stream))
                await asyncio.wait_for(waiting.wait(), 1)
                reader.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await reader
            elif mode == "error":
                with pytest.raises(ValueError, match="broken body"):
                    await anext(stream)
            else:
                rest = [event async for event in stream]
                assert rest[-1].type == "done"
                assert (rest[-1].prompt_tokens, rest[-1].completion_tokens) == (2, 3)
            assert body.closed, "HTTP response must close without waiting for GC or client shutdown"
        finally:
            if reader is not None and not reader.done():
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            await stream.aclose()
            await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("route", ["known", "initial_rejection", "retry_rejection"])
def test_responses_fallback_is_closed_with_outer_generator(monkeypatch, route):
    async def scenario():
        closed = asyncio.Event()

        async def delegated(*_args):
            try:
                yield StreamEvent(type="token", text="hello")
                await asyncio.Event().wait()
            finally:
                closed.set()

        # No SDK client is needed: both routing and fallback are local mocks.
        from types import SimpleNamespace

        create = AsyncMock(side_effect=(
            [ValueError("max_completion_tokens"), ValueError("use /v1/responses")]
            if route == "retry_rejection" else ValueError("use /v1/responses")
        ))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        monkeypatch.setattr(openai_provider, "_cached_client", lambda *_args: client)
        monkeypatch.setattr(openai_provider, "_RESPONSES_FOR_TOOLS", set())
        monkeypatch.setattr(openai_provider, "_NEEDS_MAX_COMPLETION_TOKENS", set())
        monkeypatch.setattr(openai_provider.OpenAIProvider, "_model_max_output", AsyncMock(return_value=1024))
        provider = openai_provider.OpenAIProvider(
            provider="openai", api_key="test-only", credential_scope="test",
            model="gpt-5.6" if route == "known" else "test-model",
        )
        underlying = delegated()
        monkeypatch.setattr(provider, "_responses_stream", lambda *_args: underlying)
        stream = provider.stream([], tools=[ToolSpec(name="test", description="test", parameters={})])
        try:
            async for event in stream:
                if event.type == "token":
                    break
            await stream.aclose()
            assert closed.is_set()
            assert create.await_count == {"known": 0, "initial_rejection": 1, "retry_rejection": 2}[route]
        finally:
            await stream.aclose()
            await underlying.aclose()

    asyncio.run(scenario())


def test_provider_connection_test_closes_early_success_stream(monkeypatch):
    async def scenario():
        from types import SimpleNamespace

        closed = asyncio.Event()

        async def delegated(*_args, **_kwargs):
            try:
                yield StreamEvent(type="token", text="hello")
            finally:
                closed.set()

        client = SimpleNamespace(models=SimpleNamespace(list=AsyncMock(side_effect=ValueError("no models route"))))
        monkeypatch.setattr(openai_provider, "_cached_client", lambda *_args: client)
        provider = openai_provider.OpenAIProvider(
            provider="openai_compatible", api_key="test-only", credential_scope="test", model="test-model",
        )
        underlying = delegated()
        monkeypatch.setattr(provider, "stream", lambda *_args, **_kwargs: underlying)
        try:
            assert await provider.test() == (True, "Connection OK")
            assert closed.is_set()
        finally:
            await underlying.aclose()

    asyncio.run(scenario())

