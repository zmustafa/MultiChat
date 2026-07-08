from __future__ import annotations

from .base import ToolContext, ToolDef, ToolResult

# Max characters returned per read_document call.
PAGE_CHARS = 6000


class ReadDocumentTool:
    definition = ToolDef(
        name="read_document",
        description=(
            "Read the full text of a document the user has uploaded to this "
            "conversation (PDF, Word, Excel, CSV, or text). Call with no arguments to "
            "list available documents. Provide 'name' to read a specific document, "
            "'query' to return only the passages containing a search term, or 'page' "
            "to page through long documents. Use this to answer questions grounded in "
            "the user's uploaded files."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Filename of the document to read (see the list).",
                },
                "query": {
                    "type": "string",
                    "description": "Optional search term; returns passages containing it.",
                },
                "page": {
                    "type": "integer",
                    "description": "1-based page for long documents (each page is ~6000 chars).",
                },
            },
        },
    )

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        docs = ctx.documents or []
        if not docs:
            return ToolResult(
                content="No documents have been uploaded to this conversation.",
                citations=None,
            )

        name = (args.get("name") or "").strip()
        if not name:
            listing = "\n".join(
                f"- {d['name']} ({len(d['text'])} chars)" for d in docs
            )
            return ToolResult(
                content="Available documents:\n" + listing, citations=None
            )

        # Match by exact name, then case-insensitive substring.
        doc = next((d for d in docs if d["name"] == name), None)
        if doc is None:
            lname = name.lower()
            doc = next((d for d in docs if lname in d["name"].lower()), None)
        if doc is None:
            names = ", ".join(d["name"] for d in docs)
            return ToolResult(
                content=f'Document "{name}" not found. Available: {names}',
                citations=None,
            )

        text = doc["text"]

        query = (args.get("query") or "").strip()
        if query:
            lines = text.splitlines()
            ql = query.lower()
            hits = [ln for ln in lines if ql in ln.lower()]
            if not hits:
                return ToolResult(
                    content=f'No passages containing "{query}" in "{doc["name"]}".',
                    citations=None,
                )
            joined = "\n".join(hits)
            return ToolResult(
                content=f'Passages containing "{query}" in "{doc["name"]}":\n{joined[:PAGE_CHARS]}',
                citations=None,
            )

        try:
            page = int(args.get("page") or 1)
        except (TypeError, ValueError):
            page = 1
        page = max(page, 1)
        start = (page - 1) * PAGE_CHARS
        chunk = text[start : start + PAGE_CHARS]
        total_pages = max((len(text) + PAGE_CHARS - 1) // PAGE_CHARS, 1)
        if not chunk:
            return ToolResult(
                content=f'Page {page} is beyond the end of "{doc["name"]}" '
                f"({total_pages} page(s) total).",
                citations=None,
            )
        header = f'"{doc["name"]}" — page {page}/{total_pages}:\n'
        return ToolResult(content=header + chunk, citations=None)
