from __future__ import annotations

import re

import httpx

from .base import ToolContext, ToolDef, ToolResult
from .ssrf import is_safe_url

MAX_BYTES = 2 * 1024 * 1024  # 2 MB


def _sanitize(html: str) -> str:
    # strip scripts/styles then tags
    html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
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
        url = (args.get("url") or "").strip()
        ok, reason = is_safe_url(url)
        if not ok:
            return ToolResult(content=f"Refused to fetch URL: {reason}", citations=[])
        try:
            async with httpx.AsyncClient(
                timeout=20, follow_redirects=True, max_redirects=3
            ) as client:
                async with client.stream("GET", url) as resp:
                    if resp.status_code >= 400:
                        return ToolResult(
                            content=f"Fetch failed: HTTP {resp.status_code}",
                            citations=[],
                        )
                    chunks = bytearray()
                    async for chunk in resp.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) >= MAX_BYTES:
                            break
                    raw = bytes(chunks).decode("utf-8", errors="ignore")
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"Fetch error: {exc}", citations=[])
        text = _sanitize(raw)[:8000]
        return ToolResult(
            content=text or "(empty page)",
            citations=[{"title": url, "url": url, "snippet": text[:200]}],
        )
