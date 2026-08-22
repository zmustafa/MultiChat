"""Ask a model for JSON and actually get it back.

No provider in this app exposes a native JSON mode — the mix of Copilot-proxied models and
the OpenAI Responses API is too inconsistent to rely on ``response_format``. So the shape
is stated in the prompt, recovered by a tolerant parser, and given exactly one repair
round-trip when the model gets it wrong.

A model that still cannot comply is *degraded*, not failed: its prose becomes the answer
field and it simply contributes no structured detail. One stubborn model must never take
down a whole deliberation.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session as DbSession

from .models import Provider
from .providers.registry import build_provider
from .tools.argfix import unflatten_args

# ```json ... ``` or a bare ``` ... ``` block
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\r?\n(.*?)```", re.DOTALL)
# A // comment occupying its own line (never a scheme inside a string).
_LINE_COMMENT_RE = re.compile(r"^[ \t]*//[^\n]*$", re.MULTILINE)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _balanced_span(text: str) -> str | None:
    """Return the first balanced ``{...}`` span, ignoring braces inside strings."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _loosen(text: str) -> str:
    """Fix the two JSON mistakes models actually make: line comments and trailing commas."""
    out = _LINE_COMMENT_RE.sub("", text)
    return _TRAILING_COMMA_RE.sub(r"\1", out)


def extract_json(text: str) -> dict | None:
    """Pull a JSON object out of model prose.

    Tries, in order: a fenced block, the whole text, then the first balanced ``{...}`` span
    — each both as-is and lightly repaired.
    """
    if not text:
        return None
    candidates: list[str] = [m.group(1) for m in _FENCE_RE.finditer(text)]
    stripped = text.strip()
    if stripped.startswith("{"):
        candidates.append(stripped)
    span = _balanced_span(text)
    if span:
        candidates.append(span)
    for candidate in candidates:
        for attempt in (candidate, _loosen(candidate)):
            try:
                data = json.loads(attempt)
            except Exception:  # noqa: BLE001 — try the next candidate
                continue
            if isinstance(data, dict):
                # Some models (notably Gemini via Copilot) flatten nested objects into
                # dotted keys; rebuild the intended structure.
                return unflatten_args(data)
    return None


@dataclass
class StructuredResult:
    """The outcome of one structured call. ``data`` is always usable."""

    data: dict
    raw: str = ""
    degraded: bool = False
    error: str | None = None
    latency_ms: int = 0
    ttft_ms: int | None = None
    usage: dict = field(default_factory=dict)
    attempts: int = 1


_CONTRACT = (
    "OUTPUT FORMAT — this is not optional.\n"
    "Reply with ONE fenced JSON block and nothing else. No prose before it, none after.\n\n"
    "```json\n{schema}\n```\n\n"
    "Every key shown must be present. Use null for a value you genuinely cannot give and "
    "[] for an empty list. Do not invent keys that are not in the shape. Do not wrap the "
    "block in extra commentary."
)

_REPAIR = (
    "Your previous reply could not be parsed as JSON ({error}).\n\n"
    "Send the SAME content again as one valid fenced ```json block, and nothing else. "
    "Check that every string is quoted, every brace is closed, and there are no trailing "
    "commas or comments."
)


async def _drain(
    llm: Any,
    messages: list[dict[str, Any]],
    on_token: Callable[[str], None] | None,
) -> tuple[str, dict, int | None]:
    """Collect a streamed completion into a single string."""
    text = ""
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    ttft: int | None = None
    started = time.monotonic()
    async for ev in llm.stream(messages, None):
        if ev.type == "token" and ev.text:
            if ttft is None:
                ttft = int((time.monotonic() - started) * 1000)
            text += ev.text
            if on_token:
                on_token(ev.text)
        elif ev.type == "done":
            usage["prompt_tokens"] += ev.prompt_tokens or 0
            usage["completion_tokens"] += ev.completion_tokens or 0
    return text, usage, ttft


def _missing(data: dict, required: Sequence[str]) -> list[str]:
    return [k for k in required if k not in data]


async def call_structured(
    provider: Provider,
    db: DbSession,
    model: str,
    *,
    system: str,
    user: str | list[dict[str, Any]],
    schema: str,
    required: Sequence[str] = (),
    validate: Callable[[dict], str | None] | None = None,
    on_token: Callable[[str], None] | None = None,
    repair_attempts: int = 1,
    fallback_field: str = "answer",
) -> StructuredResult:
    """Run one model call that must come back as JSON.

    ``validate`` returns an error string to trigger the repair round-trip (used to enforce
    protocol rules such as "every rejection carries a reason"), or None when the payload is
    acceptable.
    """
    started = time.monotonic()
    prompt_system = f"{system}\n\n{_CONTRACT.format(schema=schema)}"
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": prompt_system},
        {"role": "user", "content": user},
    ]

    llm = await build_provider(provider, db, model)
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0}
    raw = ""
    ttft: int | None = None
    problem = "no reply"

    for attempt in range(repair_attempts + 1):
        text, usage, first_token = await _drain(llm, messages, on_token if attempt == 0 else None)
        raw = text or raw
        total_usage["prompt_tokens"] += usage["prompt_tokens"]
        total_usage["completion_tokens"] += usage["completion_tokens"]
        if ttft is None:
            ttft = first_token

        data = extract_json(text)
        if data is None:
            problem = "no JSON object found in the reply"
        else:
            gaps = _missing(data, required)
            if gaps:
                problem = f"missing required field(s): {', '.join(gaps)}"
            else:
                problem = (validate(data) if validate else None) or ""
                if not problem:
                    return StructuredResult(
                        data=data,
                        raw=raw,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        ttft_ms=ttft,
                        usage=total_usage,
                        attempts=attempt + 1,
                    )

        if attempt >= repair_attempts:
            break
        messages = messages[:2] + [
            {"role": "assistant", "content": text[:6000]},
            {"role": "user", "content": _REPAIR.format(error=problem)},
        ]

    # Still not parseable — keep the prose so the run continues without this model's detail.
    return StructuredResult(
        data={fallback_field: raw.strip()},
        raw=raw,
        degraded=True,
        error=problem or "unparseable",
        latency_ms=int((time.monotonic() - started) * 1000),
        ttft_ms=ttft,
        usage=total_usage,
        attempts=repair_attempts + 1,
    )
