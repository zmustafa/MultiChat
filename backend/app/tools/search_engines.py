from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, unquote, urlparse

import httpx

# A realistic desktop UA so DuckDuckGo's HTML endpoint returns results.
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

SEARCH_ENGINES = ("brave", "duckduckgo")


def normalize_engine(engine: str | None) -> str:
    e = (engine or "").strip().lower()
    if e in ("ddg", "duck", "duckduckgo"):
        return "duckduckgo"
    if e == "brave":
        return "brave"
    return ""


def _strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def _decode_ddg_href(href: str) -> str:
    """DuckDuckGo HTML results wrap the target URL in a redirect like
    //duckduckgo.com/l/?uddg=<encoded>. Extract the real URL."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    try:
        parsed = urlparse(href)
        if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
            qs = parse_qs(parsed.query)
            uddg = qs.get("uddg", [None])[0]
            if uddg:
                return unquote(uddg)
    except Exception:  # noqa: BLE001
        pass
    return href


async def brave_search(query: str, count: int, api_key: str | None) -> list[dict]:
    if not api_key:
        raise RuntimeError("web_search is not configured (missing Brave API key).")
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": count},
            headers={"Accept": "application/json", "X-Subscription-Token": api_key},
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Brave search failed: HTTP {resp.status_code}")
        data = resp.json()
    out: list[dict] = []
    for item in (data.get("web", {}) or {}).get("results", [])[:count]:
        out.append(
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("description", ""),
            }
        )
    return out


async def duckduckgo_search(query: str, count: int) -> list[dict]:
    """Scrape DuckDuckGo's no-JS HTML endpoint. Requires no API key."""
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.get(
            "https://duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": _UA, "Accept": "text/html"},
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"DuckDuckGo search failed: HTTP {resp.status_code}")
        page = resp.text

    # Each result exposes an anchor with class result__a (title + href) and a sibling
    # anchor/div with class result__snippet.
    anchors = re.findall(
        r'result__a[^>]*?href="([^"]+)"[^>]*>(.*?)</a>', page, re.S
    )
    snippets = re.findall(
        r'result__snippet[^>]*?>(.*?)</a>', page, re.S
    )
    out: list[dict] = []
    for i, (href, title_html) in enumerate(anchors):
        if len(out) >= count:
            break
        url = _decode_ddg_href(html.unescape(href))
        title = _strip_html(title_html)
        snippet = _strip_html(snippets[i]) if i < len(snippets) else ""
        if not url or not title:
            continue
        out.append({"title": title, "url": url, "snippet": snippet})
    return out


async def run_search(
    engine: str, query: str, count: int, brave_api_key: str | None
) -> list[dict]:
    if normalize_engine(engine) == "duckduckgo":
        return await duckduckgo_search(query, count)
    return await brave_search(query, count, brave_api_key)
