"""Helpers for reporting failures to clients without leaking internals.

Raw exception text can contain stack traces, absolute file paths, request URLs with
embedded credentials and provider-side internals. Those belong in the server log, not in
an HTTP response. `safe_error` builds a short, log-safe description instead.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("multichat")


def safe_error(exc: BaseException) -> str:
    """A short, non-sensitive description of `exc`: its type plus an HTTP status when the
    underlying library exposes one. Never includes the exception message or traceback."""
    name = type(exc).__name__
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if not isinstance(status, int):
        status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return f"{name} (HTTP {status})"
    return name


def log_and_describe(exc: BaseException, context: str) -> str:
    """Log the full exception (with traceback) and return a safe description for clients."""
    logger.exception("%s", context)
    return safe_error(exc)
