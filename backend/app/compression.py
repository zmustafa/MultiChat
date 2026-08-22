"""Response compression that is safe for this app's streaming design.

Starlette's stock ``GZipMiddleware`` also wraps streaming responses. Deflate buffers
internally until its window fills, so an SSE stream compressed that way stops arriving
token-by-token — which would silently break live lane streaming, deliberation runs and
eval progress. This middleware therefore compresses only *complete* (single-message)
responses and passes any streaming response through untouched.

That still covers the case that matters: ``GET /api/sessions/{id}`` serialises an entire
transcript (hundreds of KB of markdown) and is refetched every time a lane finishes.
"""
from __future__ import annotations

import gzip

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Content types that gain nothing from gzip (already compressed) or must not be buffered.
_SKIP_PREFIXES = (
    "text/event-stream",
    "image/",
    "video/",
    "audio/",
    "application/zip",
    "application/gzip",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument",
)


def _should_compress(content_type: str) -> bool:
    ct = content_type.split(";", 1)[0].strip().lower()
    if not ct:
        return False
    return not ct.startswith(_SKIP_PREFIXES)


class ConditionalGZipMiddleware:
    def __init__(self, app: ASGIApp, minimum_size: int = 1024, compresslevel: int = 6) -> None:
        self.app = app
        self.minimum_size = minimum_size
        self.compresslevel = compresslevel

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if "gzip" not in Headers(scope=scope).get("accept-encoding", ""):
            await self.app(scope, receive, send)
            return

        start_message: Message | None = None
        passthrough = False

        async def send_wrapper(message: Message) -> None:
            nonlocal start_message, passthrough

            if passthrough:
                await send(message)
                return

            if message["type"] == "http.response.start":
                headers = Headers(raw=message["headers"])
                if headers.get("content-encoding") or not _should_compress(
                    headers.get("content-type", "")
                ):
                    passthrough = True
                    await send(message)
                    return
                # Hold the start message: the body decides whether we compress.
                start_message = message
                return

            if message["type"] != "http.response.body" or start_message is None:
                await send(message)
                return

            body = message.get("body", b"")
            if message.get("more_body", False):
                # Streaming response — never buffer it.
                passthrough = True
                await send(start_message)
                start_message = None
                await send(message)
                return

            if len(body) < self.minimum_size:
                await send(start_message)
                start_message = None
                await send(message)
                return

            compressed = gzip.compress(body, compresslevel=self.compresslevel)
            mutable = MutableHeaders(raw=start_message["headers"])
            mutable["Content-Encoding"] = "gzip"
            mutable["Content-Length"] = str(len(compressed))
            mutable.add_vary_header("Accept-Encoding")
            await send(start_message)
            start_message = None
            await send({"type": "http.response.body", "body": compressed, "more_body": False})

        await self.app(scope, receive, send_wrapper)
