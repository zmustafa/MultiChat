"""Dev-only request instrumentation: wall time, SQL statement count and response size.

Enabled with ``PERF_LOG=1``. Off by default so production paths pay nothing — the
SQLAlchemy event hook is only registered when the flag is set.

The statement counter is a process-wide monotonic total that callers sample before and
after a region. A ContextVar would be more precise per request, but Starlette runs the
downstream app in its own task, so a request-scoped value set in middleware never sees the
queries the router issues. Under concurrent load the per-request number is therefore
"statements executed while this request was in flight"; in the (sequential) tests it is
exact, which is what the query-budget assertions rely on.
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import event
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

logger = logging.getLogger("multichat.perf")

_total = 0
_hooks_installed = False


def current_query_count() -> int:
    return _total


def install_query_counter(engine) -> None:  # noqa: ANN001 - SQLAlchemy Engine
    """Count every statement the engine executes."""
    global _hooks_installed
    if _hooks_installed:
        return
    _hooks_installed = True

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        global _total
        _total += 1


class PerfLogMiddleware(BaseHTTPMiddleware):
    """Log ``method path -> status | Nms | Q queries | B bytes`` for each request."""

    def __init__(self, app: ASGIApp, slow_ms: float = 0.0) -> None:
        super().__init__(app)
        self._slow_ms = slow_ms

    async def dispatch(self, request, call_next):  # noqa: ANN001
        before = current_query_count()
        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        if elapsed_ms >= self._slow_ms:
            logger.info(
                "%s %s -> %s | %.1f ms | %d queries | %s bytes",
                request.method,
                request.url.path,
                response.status_code,
                elapsed_ms,
                current_query_count() - before,
                response.headers.get("content-length", "stream"),
            )
        return response


class query_counter:
    """Context manager reporting the number of statements executed inside the block.

    Used by the query-budget tests, which assert that hot endpoints stay under a fixed
    number of queries so an N+1 cannot creep back in unnoticed.
    """

    def __init__(self) -> None:
        self.count = 0
        self._start = 0

    def __enter__(self) -> query_counter:
        self._start = current_query_count()
        return self

    def __exit__(self, *_exc) -> None:
        self.count = current_query_count() - self._start
