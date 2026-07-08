from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

# OpenAI-style message dict. Kept as a loose alias for readability/compatibility.
ChatMessage = dict[str, Any]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema


@dataclass
class ToolCallRequest:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class StreamEvent:
    """One event emitted while the model generates a response."""

    type: str  # "token" | "tool_calls" | "done" | "status"
    text: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # For type=="status": a short machine phase ("connecting" | "request_sent" |
    # "response") so the UI can pick an icon; `text` carries the human message.
    phase: str = ""


class LLMProvider(ABC):
    """Provider interface: streaming chat completion with tool calling."""

    @abstractmethod
    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a model response. Yields token events, then optionally a
        tool_calls event, then a final done event with usage."""
        raise NotImplementedError
        yield  # pragma: no cover

    async def list_models(self) -> list[str]:
        """Best-effort model listing; adapters may return a configured fallback."""
        return []

    async def test(self) -> tuple[bool, str]:
        """Lightweight connectivity/credential check."""
        try:
            await self.list_models()
            return True, "Connection OK"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
