from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..db import get_db
from ..models import (
    Lane,
    LaneMessage,
    Provider,
    Session as ChatSession,
    ToolCall,
    Turn,
    User,
)
from ..security import current_user

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


# Rough public per-model rates (USD per 1M tokens: input, output). Prefix-matched.
# Used only for a visibility estimate — NOT billing. Standard (non-batch, non-cached,
# short-context) list prices; verified against the public pricing pages on 2026-08-08:
#   OpenAI    https://developers.openai.com/api/docs/pricing
#   Anthropic https://platform.claude.com/docs/en/about-claude/pricing
#   Google    https://ai.google.dev/gemini-api/docs/pricing
# Order matters: entries are substring-matched, so put the more specific name first.
_PRICES: list[tuple[str, float, float]] = [
    # --- OpenAI ---
    ("gpt-5.6-sol", 5.00, 30.00),
    ("gpt-5.6-terra", 2.00, 12.00),
    ("gpt-5.6-luna", 0.20, 1.20),
    ("gpt-5.5-pro", 30.00, 180.00),
    ("gpt-5.5", 5.00, 30.00),
    ("gpt-5.4-pro", 30.00, 180.00),
    ("gpt-5.4-mini", 0.75, 4.50),
    ("gpt-5.4-nano", 0.20, 1.25),
    ("gpt-5.4", 2.50, 15.00),
    ("gpt-5.3", 1.75, 14.00),
    ("gpt-5.2-pro", 21.00, 168.00),
    ("gpt-5.2", 1.75, 14.00),
    ("gpt-5.1", 1.25, 10.00),
    ("gpt-5-pro", 15.00, 120.00),
    ("gpt-5-mini", 0.25, 2.00),
    ("gpt-5-nano", 0.05, 0.40),
    ("gpt-5", 1.25, 10.00),
    ("gpt-4.1-mini", 0.40, 1.60),
    ("gpt-4.1-nano", 0.10, 0.40),
    ("gpt-4.1", 2.00, 8.00),
    ("gpt-4o-mini", 0.15, 0.60),
    ("gpt-4o", 2.50, 10.00),
    ("o4-mini", 1.10, 4.40),
    ("o3-pro", 20.00, 80.00),
    ("o3-mini", 1.10, 4.40),
    ("o3", 2.00, 8.00),
    # --- Anthropic ---
    ("claude-fable", 10.00, 50.00),
    ("claude-mythos", 10.00, 50.00),
    ("claude-opus-4-1", 15.00, 75.00),
    ("claude-opus", 5.00, 25.00),  # Opus 4.5 through Opus 5
    # Sonnet 5 is on introductory $2/$10 until 2026-08-31, then $3/$15 like Sonnet 4.x.
    ("claude-sonnet", 3.00, 15.00),
    ("claude-haiku", 1.00, 5.00),
    ("claude-3-7-sonnet", 3.00, 15.00),
    ("claude-3-5-sonnet", 3.00, 15.00),
    ("claude-3-5-haiku", 0.80, 4.00),
    ("claude-3-haiku", 0.25, 1.25),
    # --- Google ---
    ("gemini-3.6-flash", 1.50, 7.50),
    ("gemini-3.5-flash-lite", 0.30, 2.50),
    ("gemini-3.5-flash", 1.50, 9.00),
    ("gemini-3.1-flash-lite", 0.25, 1.50),
    ("gemini-3.1-pro", 2.00, 12.00),
    ("gemini-3-flash", 0.50, 3.00),
    ("gemini-3-pro", 2.00, 12.00),
    ("gemini-2.5-flash-lite", 0.10, 0.40),
    ("gemini-2.5-flash", 0.30, 2.50),
    ("gemini-2.5-pro", 1.25, 10.00),
    ("gemini-2.0-flash-lite", 0.075, 0.30),
    ("gemini-2.0-flash", 0.10, 0.40),
    ("gemini-1.5-flash", 0.075, 0.30),
    ("gemini-1.5-pro", 1.25, 5.00),
    ("gemini", 0.30, 2.50),
]
_DEFAULT_PRICE = (1.50, 7.50)

# Tool-name substrings that indicate a mutating ("write") action; else "read".
_WRITE_HINTS = (
    "create", "update", "delete", "write", "put", "post", "patch",
    "deploy", "set", "remove", "add", "edit", "insert", "upload", "send",
    "restart", "start", "stop", "scale", "provision",
)


def _rate(model: str) -> tuple[float, float]:
    m = (model or "").lower()
    for prefix, inp, out in _PRICES:
        if prefix in m:
            return inp, out
    return _DEFAULT_PRICE


def _cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    inp, out = _rate(model)
    return prompt_tokens / 1_000_000 * inp + completion_tokens / 1_000_000 * out


def _kind(tool_name: str) -> str:
    n = (tool_name or "").lower()
    return "Write" if any(h in n for h in _WRITE_HINTS) else "Read"


def _as_utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _blank() -> dict:
    return {
        "responses": 0,
        "errors": 0,
        "completion_tokens": 0,
        "prompt_tokens": 0,
        "latency_ms_sum": 0,
        "latency_count": 0,
        "tokps_sum": 0.0,
        "tokps_count": 0,
        "cost": 0.0,
    }


@router.get("/usage")
def usage(
    days: int = 7,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> dict:
    """Aggregate usage across the user's sessions within the last ``days`` (0 = all time):
    per-model / per-provider / daily response stats plus tool-call, chat, token/cost,
    activity time-series and punch-card breakdowns for the Insights dashboard."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days) if days and days > 0 else None

    def _in_range(ts: datetime | None) -> bool:
        if cutoff is None or ts is None:
            return True
        return _as_utc(ts) >= cutoff

    # ---- Assistant responses (usage_json / latency / errors) ----
    rows = db.execute(
        select(LaneMessage, Lane.model, Lane.provider_id, Lane.session_id)
        .join(Lane, Lane.id == LaneMessage.lane_id)
        .join(ChatSession, ChatSession.id == Lane.session_id)
        .where(ChatSession.user_id == user.id, LaneMessage.role == "assistant")
    ).all()

    provider_names = {
        p.id: p.name
        for p in db.scalars(select(Provider).where(Provider.user_id == user.id)).all()
    }

    by_model: dict[str, dict] = {}
    by_provider: dict[str, dict] = {}
    by_day: dict[str, dict] = {}
    totals = _blank()
    prompt_total = 0
    completion_total = 0
    requests = 0
    cost_by_model: dict[str, dict] = {}
    session_events: dict[str, int] = {}
    punch: dict[str, int] = {}  # "weekday:hour" -> count

    for m, model, provider_id, session_id in rows:
        if not _in_range(m.created_at):
            continue
        usage_json = m.usage_json or {}
        ct = int(usage_json.get("completion_tokens") or 0)
        pt = int(usage_json.get("prompt_tokens") or 0)
        is_err = bool(m.error)
        c = _cost(model, pt, ct)

        def apply(agg: dict) -> None:
            agg["responses"] += 1
            if is_err:
                agg["errors"] += 1
            agg["completion_tokens"] += ct
            agg["prompt_tokens"] += pt
            agg["cost"] += c
            if m.latency_ms:
                agg["latency_ms_sum"] += m.latency_ms
                agg["latency_count"] += 1
            if ct and m.latency_ms:
                agg["tokps_sum"] += ct / (m.latency_ms / 1000)
                agg["tokps_count"] += 1

        apply(by_model.setdefault(model, _blank()))
        pname = provider_names.get(provider_id, "unknown")
        apply(by_provider.setdefault(pname, _blank()))
        created = _as_utc(m.created_at or now)
        apply(by_day.setdefault(created.strftime("%Y-%m-%d"), _blank()))
        apply(totals)

        prompt_total += pt
        completion_total += ct
        requests += 1
        cm = cost_by_model.setdefault(model, {"cost": 0.0, "tokens": 0})
        cm["cost"] += c
        cm["tokens"] += pt + ct

        if session_id:
            session_events[session_id] = session_events.get(session_id, 0) + 1
        punch[f"{created.weekday()}:{created.hour}"] = (
            punch.get(f"{created.weekday()}:{created.hour}", 0) + 1
        )

    # ---- Tool calls ----
    tool_rows = db.execute(
        select(ToolCall.tool_name, ToolCall.status, ToolCall.created_at, Lane.session_id)
        .join(LaneMessage, LaneMessage.id == ToolCall.lane_message_id)
        .join(Lane, Lane.id == LaneMessage.lane_id)
        .join(ChatSession, ChatSession.id == Lane.session_id)
        .where(ChatSession.user_id == user.id)
    ).all()

    tools_total = 0
    tool_by_status: dict[str, int] = {}
    tool_by_name: dict[str, int] = {}
    tool_by_kind: dict[str, int] = {}
    tool_by_day: dict[str, int] = {}
    tool_ok = 0
    tool_done = 0

    for tool_name, status, created_at, session_id in tool_rows:
        if not _in_range(created_at):
            continue
        tools_total += 1
        st = status or "running"
        tool_by_status[st] = tool_by_status.get(st, 0) + 1
        tool_by_name[tool_name] = tool_by_name.get(tool_name, 0) + 1
        tool_by_kind[_kind(tool_name)] = tool_by_kind.get(_kind(tool_name), 0) + 1
        created = _as_utc(created_at or now)
        tool_by_day[created.strftime("%Y-%m-%d")] = (
            tool_by_day.get(created.strftime("%Y-%m-%d"), 0) + 1
        )
        if st in ("ok", "error", "done", "failed"):
            tool_done += 1
            if st in ("ok", "done"):
                tool_ok += 1
        if session_id:
            session_events[session_id] = session_events.get(session_id, 0) + 1
        punch[f"{created.weekday()}:{created.hour}"] = (
            punch.get(f"{created.weekday()}:{created.hour}", 0) + 1
        )

    # ---- Chats + user messages (turns) ----
    session_meta = {
        s.id: s
        for s in db.scalars(
            select(ChatSession).where(
                ChatSession.user_id == user.id,
                ChatSession.trashed == False,  # noqa: E712
            )
        ).all()
    }
    turn_rows = db.execute(
        select(Turn.session_id, Turn.created_at)
        .join(ChatSession, ChatSession.id == Turn.session_id)
        .where(ChatSession.user_id == user.id)
    ).all()
    messages_total = 0
    for session_id, created_at in turn_rows:
        if not _in_range(created_at):
            continue
        messages_total += 1
        if session_id:
            session_events[session_id] = session_events.get(session_id, 0) + 1

    chats_total = sum(1 for sid in session_meta if session_events.get(sid, 0) > 0)

    # ---- Daily series (merge responses + tool calls) ----
    daily = []
    for d in sorted(set(by_day) | set(tool_by_day)):
        agg = by_day.get(d, _blank())
        daily.append(
            {
                "date": d,
                "responses": agg["responses"],
                "errors": agg["errors"],
                "error_rate": round(agg["errors"] / agg["responses"], 3)
                if agg["responses"]
                else 0,
                "completion_tokens": agg["completion_tokens"],
                "prompt_tokens": agg["prompt_tokens"],
                "tool_calls": tool_by_day.get(d, 0),
                "avg_latency_ms": round(agg["latency_ms_sum"] / agg["latency_count"])
                if agg["latency_count"]
                else None,
                "avg_tok_per_sec": round(agg["tokps_sum"] / agg["tokps_count"], 1)
                if agg["tokps_count"]
                else None,
            }
        )

    # ---- 24h activity (hourly buckets) ----
    buckets: dict[str, dict] = {}
    start = now - timedelta(hours=23)
    for i in range(24):
        h = start + timedelta(hours=i)
        buckets[h.strftime("%Y-%m-%d %H")] = {
            "label": h.strftime("%H:00"),
            "messages": 0,
            "tool_calls": 0,
        }
    for m, _model, _pid, _sid in rows:
        if m.created_at and (now - _as_utc(m.created_at)) <= timedelta(hours=24):
            key = _as_utc(m.created_at).strftime("%Y-%m-%d %H")
            if key in buckets:
                buckets[key]["messages"] += 1
    for tool_name, status, created_at, _sid in tool_rows:
        if created_at and (now - _as_utc(created_at)) <= timedelta(hours=24):
            key = _as_utc(created_at).strftime("%Y-%m-%d %H")
            if key in buckets:
                buckets[key]["tool_calls"] += 1
    activity_24h = list(buckets.values())

    def finalize(name_key: str, name: str, agg: dict) -> dict:
        return {
            name_key: name,
            "responses": agg["responses"],
            "errors": agg["errors"],
            "error_rate": round(agg["errors"] / agg["responses"], 3)
            if agg["responses"]
            else 0,
            "completion_tokens": agg["completion_tokens"],
            "prompt_tokens": agg["prompt_tokens"],
            "cost": round(agg["cost"], 4),
            "avg_latency_ms": round(agg["latency_ms_sum"] / agg["latency_count"])
            if agg["latency_count"]
            else None,
            "avg_tok_per_sec": round(agg["tokps_sum"] / agg["tokps_count"], 1)
            if agg["tokps_count"]
            else None,
        }

    active_chats = sorted(
        (
            {
                "id": sid,
                "title": session_meta[sid].title if sid in session_meta else "Untitled",
                "events": events,
                "updated_at": session_meta[sid].updated_at.isoformat()
                if sid in session_meta and session_meta[sid].updated_at
                else None,
            }
            for sid, events in session_events.items()
            if sid in session_meta and events > 0
        ),
        key=lambda x: x["events"],
        reverse=True,
    )[:6]

    total_cost = round(sum(v["cost"] for v in cost_by_model.values()), 2)

    return {
        # existing keys (backward compatible)
        "models": sorted(
            (finalize("model", k, v) for k, v in by_model.items()),
            key=lambda x: x["responses"],
            reverse=True,
        ),
        "providers": sorted(
            (finalize("provider", k, v) for k, v in by_provider.items()),
            key=lambda x: x["responses"],
            reverse=True,
        ),
        "daily": daily,
        "totals": finalize("label", "All", totals),
        # new keys
        "range_days": days,
        "kpis": {
            "messages": messages_total,
            "responses": requests,
            "tool_calls": tools_total,
            "chats": chats_total,
            "prompt_tokens": prompt_total,
            "completion_tokens": completion_total,
            "total_tokens": prompt_total + completion_total,
            "estimated_cost": total_cost,
        },
        "tools": {
            "total": tools_total,
            "by_status": sorted(
                ({"label": k, "count": v} for k, v in tool_by_status.items()),
                key=lambda x: x["count"],
                reverse=True,
            ),
            "by_kind": sorted(
                ({"label": k, "count": v} for k, v in tool_by_kind.items()),
                key=lambda x: x["count"],
                reverse=True,
            ),
            "top": sorted(
                ({"name": k, "count": v} for k, v in tool_by_name.items()),
                key=lambda x: x["count"],
                reverse=True,
            )[:10],
            "success_rate": round(tool_ok / tool_done, 3) if tool_done else None,
            "succeeded": tool_ok,
            "completed": tool_done,
        },
        "tokens": {
            "prompt": prompt_total,
            "completion": completion_total,
            "total": prompt_total + completion_total,
            "requests": requests,
            "estimated_cost": total_cost,
        },
        "cost_by_model": sorted(
            (
                {"model": k, "cost": round(v["cost"], 4), "tokens": v["tokens"]}
                for k, v in cost_by_model.items()
            ),
            key=lambda x: x["cost"],
            reverse=True,
        ),
        "activity_24h": activity_24h,
        "punchcard": [
            {"weekday": int(k.split(":")[0]), "hour": int(k.split(":")[1]), "count": v}
            for k, v in punch.items()
        ],
        "active_chats": active_chats,
    }
