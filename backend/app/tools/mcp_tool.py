from __future__ import annotations

from .base import Tool, ToolContext, ToolDef, ToolResult
from ..mcp.workiq import workiq


class WorkIqTool(Tool):
    """Adapts a discovered Work IQ MCP tool to the MultiChat Tool interface."""

    def __init__(self, name: str, description: str, parameters: dict):
        self.definition = ToolDef(name=name, description=description, parameters=parameters)

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:  # noqa: ARG002
        text = await workiq.call(self.definition.name, args or {})
        return ToolResult(content=text)


def workiq_tools() -> dict[str, Tool]:
    """Current Work IQ tools as {name: Tool}, empty when not connected/enabled."""
    if not (workiq.enabled and workiq.connected):
        return {}
    out: dict[str, Tool] = {}
    for spec in workiq.exposed_tools():
        out[spec["name"]] = WorkIqTool(
            spec["name"], spec["description"], spec["parameters"]
        )
    return out
