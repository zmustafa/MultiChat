from __future__ import annotations

import re

import httpx

from .base import ToolContext, ToolDef, ToolResult
from .ssrf import MAX_REDIRECTS, PublicAsyncHTTPTransport, UnsafeURL

MAX_BYTES = 2 * 1024 * 1024  # 2 MB

# Strip raw <script>/<style> bodies and comments before dropping the remaining tags.
# Each pattern tolerates attributes, arbitrary whitespace before the closing ">", and an
# unterminated element at end of input, so a crafted page can't smuggle script text
# through into the extracted plain text.
_COMMENT_RE = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?(?:</script[^>]*>|\Z)", re.IGNORECASE | re.DOTALL)
_STYLE_RE = re.compile(r"<style\b[^>]*>.*?(?:</style[^>]*>|\Z)", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]*>|<[^>]*\Z")


def _sanitize(html: str) -> str:
    # strip comments, scripts and styles (content included), then the remaining tags
    html = _COMMENT_RE.sub(" ", html)
    html = _SCRIPT_RE.sub(" ", html)
    html = _STYLE_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


class FetchUrlTool:
    definition = ToolDef(
        name="fetch_url",
        description="Fetch a public web page and return its readable text content.",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The http/https URL to fetch"},
            },
            "required": ["url"],
        },
    )

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        value = args.get("url")
        url = value.strip() if isinstance(value, str) else ""
        target = url
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False,
                                         transport=PublicAsyncHTTPTransport()) as client:
                for redirects in range(MAX_REDIRECTS + 1):
                    async with client.stream("GET", target) as resp:
                        if resp.has_redirect_location:
                            if redirects == MAX_REDIRECTS:
                                return ToolResult(content="Fetch failed: too many redirects", citations=[])
                            # Resolve against this hop's request, then validate before
                            # issuing the next request (including same-host redirects).
                            target = str(resp.url.join(resp.headers["location"]))
                            continue
                        if not resp.is_success:
                            return ToolResult(
                                content=f"Fetch failed: HTTP {resp.status_code}",
                                citations=[],
                            )
                        chunks = bytearray()
                        async for chunk in resp.aiter_bytes():
                            # Never append an entire oversized transport/decoded chunk.
                            remaining = MAX_BYTES - len(chunks)
                            chunks.extend(chunk[:remaining])
                            if len(chunks) >= MAX_BYTES:
                                break
                        raw = bytes(chunks).decode("utf-8", errors="ignore")
                        break
        except UnsafeURL as exc:
            return ToolResult(content=f"Refused to fetch URL: {exc}", citations=[])
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            return ToolResult(content=f"Fetch error: {exc}", citations=[])
        text = _sanitize(raw)[:8000]
        return ToolResult(
            content=text or "(empty page)",
            citations=[{"title": url, "url": url, "snippet": text[:200]}],
        )
