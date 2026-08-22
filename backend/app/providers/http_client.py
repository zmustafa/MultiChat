"""Shared, pooled httpx clients for the hand-rolled provider adapters.

``async with httpx.AsyncClient(...)`` per call tears the connection pool down after every
request, so each lane paid a fresh TLS handshake on every message. These clients are
long-lived and keyed by timeout, and are closed once on application shutdown.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from ..config import settings

_clients: dict[float, httpx.AsyncClient] = {}
_lock = asyncio.Lock()

# Enough headroom for a broadcast fanning out to every lane at once plus tool fetches.
_LIMITS = httpx.Limits(max_connections=64, max_keepalive_connections=32)


def stream_timeout() -> httpx.Timeout:
    return httpx.Timeout(settings.LLM_REQUEST_TIMEOUT, connect=15.0)


async def get_client(timeout: float | httpx.Timeout | None = None) -> httpx.AsyncClient:
    """Return the pooled client for ``timeout`` (creating it on first use)."""
    resolved = timeout if timeout is not None else stream_timeout()
    key = resolved.read if isinstance(resolved, httpx.Timeout) else float(resolved)
    client = _clients.get(key)
    if client is not None and not client.is_closed:
        return client
    async with _lock:
        client = _clients.get(key)
        if client is not None and not client.is_closed:
            return client
        client = httpx.AsyncClient(timeout=resolved, limits=_LIMITS)
        _clients[key] = client
        return client


async def close_clients() -> None:
    clients = list(_clients.values())
    _clients.clear()
    for client in clients:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass


@asynccontextmanager
async def borrow(
    timeout: float | httpx.Timeout | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    """Drop-in replacement for ``async with httpx.AsyncClient(...)`` that leases the
    pooled client instead of building and tearing down a new one."""
    yield await get_client(timeout)
