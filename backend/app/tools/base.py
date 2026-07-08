from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict


@dataclass
class ToolResult:
    content: str
    citations: list[dict] | None = None


@dataclass
class ToolContext:
    user_id: str
    brave_api_key: str | None = None
    search_engine: str | None = None
    options: dict[str, Any] | None = None
    image_api_key: str | None = None
    image_base_url: str | None = None
    image_model: str | None = None
    documents: list[dict[str, Any]] | None = None


@runtime_checkable
class Tool(Protocol):
    definition: ToolDef

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult: ...
