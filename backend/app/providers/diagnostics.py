"""Staged connection diagnostics for providers (ported concept from aznetagent).

Runs a provider connectivity check as a *pipeline* of phases and streams each phase
result so the UI can render a live diagnostics panel:

  Test:    config -> endpoint (DNS) -> connect (TCP/TLS) -> auth -> request -> first_token -> complete
  Models:  config -> endpoint (DNS) -> connect (TCP/TLS) -> fetch -> complete
"""
from __future__ import annotations

import asyncio
import socket
from typing import Any, AsyncIterator
from urllib.parse import urlparse

from sqlalchemy.orm import Session as DbSession

from ..models import Provider
from .registry import _resolve_model, build_provider

# Default host:port per provider type when no base_url is configured.
_DEFAULT_ENDPOINTS = {
    "openai": ("api.openai.com", 443),
    "openai_eu": ("eu.api.openai.com", 443),
    "anthropic": ("api.anthropic.com", 443),
    "gemini": ("generativelanguage.googleapis.com", 443),
    "github_copilot": ("api.githubcopilot.com", 443),
    "ollama": ("localhost", 11434),
}

_FALLBACK_MODEL = {
    "openai": "gpt-4o-mini",
    "openai_eu": "gpt-4o-mini",
    "azure_openai": "gpt-4o",
    "azure_foundry": "gpt-4o",
    "anthropic": "claude-3-5-sonnet-latest",
    "gemini": "gemini-1.5-flash",
}


def _step(step: str, status: str, title: str, detail: str = "") -> dict[str, Any]:
    return {"step": step, "status": status, "title": title, "detail": detail}


def _endpoint(provider: Provider) -> tuple[str, int]:
    extra = provider.extra_json or {}
    base = provider.base_url or extra.get("copilot_api_base") or ""
    if base:
        parsed = urlparse(base if "://" in base else f"https://{base}")
        host = parsed.hostname or ""
        port = parsed.port or (443 if (parsed.scheme or "https") == "https" else 80)
        if host:
            return host, port
    return _DEFAULT_ENDPOINTS.get(provider.provider_type, ("api.openai.com", 443))


async def _dns(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None)
    return sorted({i[4][0] for i in infos})


async def _tcp_connect(host: str, port: int) -> None:
    fut = asyncio.open_connection(host, port)
    reader, writer = await asyncio.wait_for(fut, timeout=10)
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:  # noqa: BLE001
        pass


def _model_for(provider: Provider) -> str:
    return _resolve_model(provider, None) or _FALLBACK_MODEL.get(
        provider.provider_type, ""
    )


async def test_stream(provider: Provider, db: DbSession) -> AsyncIterator[dict[str, Any]]:
    model = _model_for(provider)
    yield _step(
        "config", "ok", "Load configuration",
        f"{provider.provider_type} · {provider.auth_method}"
        + (f" · {model}" if model else ""),
    )

    host, port = _endpoint(provider)
    try:
        addrs = await _dns(host)
        yield _step("endpoint", "ok", "Resolve endpoint (DNS)", f"{host} → {', '.join(addrs[:3])}")
    except Exception as exc:  # noqa: BLE001
        yield _step("endpoint", "error", "Resolve endpoint (DNS)", f"{host}: {exc}")
        yield {"done": True, "ok": False, "detail": f"DNS failed for {host}"}
        return

    try:
        await _tcp_connect(host, port)
        yield _step("connect", "ok", "Connect (TCP / TLS)", f"{host}:{port}")
    except Exception as exc:  # noqa: BLE001
        yield _step("connect", "error", "Connect (TCP / TLS)", f"{host}:{port}: {exc}")
        yield {"done": True, "ok": False, "detail": f"Could not connect to {host}:{port}"}
        return

    # auth + request + first_token via a tiny streamed generation.
    try:
        llm = await build_provider(provider, db, model)
    except Exception as exc:  # noqa: BLE001
        yield _step("auth", "error", "Authenticate", str(exc))
        yield {"done": True, "ok": False, "detail": str(exc)}
        return

    auth_ok = False
    request_ok = False
    got_token = False
    try:
        async for ev in llm.stream([{"role": "user", "content": "ping"}], max_tokens=1):
            if ev.type == "status":
                if ev.phase == "request_sent" and not auth_ok:
                    auth_ok = True
                    yield _step("auth", "ok", "Authenticate", "credentials accepted")
                    request_ok = True
                    yield _step("request", "ok", "Send probe request", "request sent")
            elif ev.type in ("token", "tool_calls", "done"):
                if not auth_ok:
                    auth_ok = True
                    yield _step("auth", "ok", "Authenticate", "credentials accepted")
                if not request_ok:
                    request_ok = True
                    yield _step("request", "ok", "Send probe request", "request sent")
                got_token = True
                sample = (ev.text or "").strip()[:40] if ev.type == "token" else "response"
                yield _step("first_token", "ok", "Receive first token", sample or "ok")
                break
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        phase = "auth" if "401" in msg or "403" in msg or "key" in msg.lower() else "request"
        yield _step(phase, "error", "Authenticate" if phase == "auth" else "Send probe request", msg)
        yield {"done": True, "ok": False, "detail": msg}
        return

    if not got_token:
        yield _step("first_token", "warn", "Receive first token", "no token returned")
    yield _step("complete", "ok", "Complete", "connection healthy")
    yield {"done": True, "ok": True, "detail": "Connection healthy"}


async def models_stream(provider: Provider, db: DbSession) -> AsyncIterator[dict[str, Any]]:
    yield _step("config", "ok", "Load configuration", provider.provider_type)

    host, port = _endpoint(provider)
    try:
        addrs = await _dns(host)
        yield _step("endpoint", "ok", "Resolve endpoint (DNS)", f"{host} → {', '.join(addrs[:3])}")
    except Exception as exc:  # noqa: BLE001
        yield _step("endpoint", "error", "Resolve endpoint (DNS)", f"{host}: {exc}")
        yield {"done": True, "ok": False, "detail": f"DNS failed for {host}", "models": []}
        return

    try:
        await _tcp_connect(host, port)
        yield _step("connect", "ok", "Connect (TCP / TLS)", f"{host}:{port}")
    except Exception as exc:  # noqa: BLE001
        yield _step("connect", "error", "Connect (TCP / TLS)", f"{host}:{port}: {exc}")
        yield {"done": True, "ok": False, "detail": "Could not connect", "models": []}
        return

    try:
        llm = await build_provider(provider, db)
        models = await llm.list_models()
        yield _step("fetch", "ok", "Fetch model catalogue", f"{len(models)} models")
        yield _step("complete", "ok", "Complete", "")
        yield {"done": True, "ok": True, "detail": f"{len(models)} models", "models": models}
    except Exception as exc:  # noqa: BLE001
        yield _step("fetch", "error", "Fetch model catalogue", str(exc))
        yield {"done": True, "ok": False, "detail": str(exc), "models": []}
