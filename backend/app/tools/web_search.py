from __future__ import annotations

from .base import Tool, ToolContext, ToolDef, ToolResult
from .search_engines import normalize_engine, run_search


class WebSearchTool:
    definition = ToolDef(
        name="web_search",
        description="Search the web for up-to-date information. Returns a list of "
        "relevant results with titles, URLs, and snippets.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
            },
            "required": ["query"],
        },
    )

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        query = (args.get("query") or "").strip()
        if not query:
            return ToolResult(content="No query provided.", citations=[])

        # Resolve the engine: explicit setting, else Brave if a key exists, else DDG.
        engine = normalize_engine(ctx.search_engine)
        if not engine:
            engine = "brave" if ctx.brave_api_key else "duckduckgo"

        count = int((ctx.options or {}).get("count", 5))
        try:
            citations = await run_search(engine, query, count, ctx.brave_api_key)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"Search failed: {exc}", citations=[])

        if not citations:
            return ToolResult(content="No results found.", citations=[])
        lines = [
            f"{i + 1}. {c['title']}\n{c['url']}\n{c['snippet']}"
            for i, c in enumerate(citations)
        ]
        return ToolResult(content="\n\n".join(lines), citations=citations)


_: Tool = WebSearchTool()

