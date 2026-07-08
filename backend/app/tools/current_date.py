from __future__ import annotations

from datetime import datetime, timezone

from .base import ToolContext, ToolDef, ToolResult


class CurrentDateTool:
    definition = ToolDef(
        name="current_date",
        description="Get the current date and time (UTC). Use this whenever the user "
        "asks about today, now, the current year, or anything time-relative.",
        parameters={"type": "object", "properties": {}},
    )

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        now = datetime.now(timezone.utc)
        return ToolResult(
            content=now.strftime("%A, %d %B %Y, %H:%M UTC"), citations=None
        )
