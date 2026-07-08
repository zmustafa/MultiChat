from __future__ import annotations

import base64
import json
import os

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession, selectinload

from ..broadcast import build_lane_history, multiplex, request_stop, sse
from ..config import settings
from ..db import get_db
from ..documents import document_prompt_block
from ..export import export_session as build_export_file
from ..models import (
    Attachment,
    GeneratedFile,
    Lane,
    LaneMessage,
    Provider,
    Session as ChatSession,
    ToolCall,
    Turn,
    User,
)
from ..providers.base import ChatMessage
from ..providers.registry import build_provider, pick_default_provider
from ..schemas import (
    AttachmentOut,
    AutoTitleOut,
    BroadcastRequest,
    JudgeRequest,
    LaneCreate,
    LaneMessageOut,
    LaneOut,
    LaneUpdate,
    RegenerateRequest,
    SessionCreate,
    SessionDetail,
    SearchHit,
    SessionListItem,
    SessionUpdate,
    ToolCallOut,
    TurnOut,
)
from ..security import current_user

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


# ---------------- helpers ----------------


def _get_session(db: DbSession, user: User, session_id: str) -> ChatSession:
    s = db.get(ChatSession, session_id)
    if not s or s.user_id != user.id:
        raise HTTPException(status_code=404, detail="Session not found")
    return s


def _get_lane(db: DbSession, session: ChatSession, lane_id: str) -> Lane:
    lane = db.get(Lane, lane_id)
    if not lane or lane.session_id != session.id:
        raise HTTPException(status_code=404, detail="Lane not found")
    return lane


def _verify_provider(db: DbSession, user: User, provider_id: str) -> Provider:
    p = db.get(Provider, provider_id)
    if not p or p.user_id != user.id:
        raise HTTPException(status_code=404, detail="Provider not found")
    return p


def _attachment_out(att: Attachment) -> AttachmentOut:
    return AttachmentOut(
        id=att.id,
        filename=att.filename,
        mime_type=att.mime_type,
        size_bytes=att.size_bytes,
        kind=att.kind,
        url=f"/api/uploads/{att.id}",
    )


def _serialize_detail(db: DbSession, s: ChatSession) -> SessionDetail:
    lanes = sorted(s.lanes, key=lambda x: x.position)
    turns = sorted(s.turns, key=lambda x: x.order_index)
    messages = db.scalars(
        select(LaneMessage)
        .join(Lane, Lane.id == LaneMessage.lane_id)
        .where(Lane.session_id == s.id)
        .options(selectinload(LaneMessage.tool_calls))
        .order_by(LaneMessage.order_index)
    ).all()
    return SessionDetail(
        id=s.id,
        title=s.title,
        system_prompt=s.system_prompt,
        tools_enabled=s.tools_enabled,
        tool_config_json=dict(s.tool_config_json or {}),
        folder_id=s.folder_id,
        pinned=bool(s.pinned),
        archived=bool(s.archived),
        created_at=s.created_at,
        updated_at=s.updated_at,
        lanes=[LaneOut.model_validate(l) for l in lanes],
        turns=[
            TurnOut(
                id=t.id,
                session_id=t.session_id,
                order_index=t.order_index,
                content=t.content,
                target_lane_ids_json=t.target_lane_ids_json,
                created_at=t.created_at,
                attachments=[_attachment_out(a) for a in t.attachments],
            )
            for t in turns
        ],
        messages=[
            LaneMessageOut(
                id=m.id,
                lane_id=m.lane_id,
                turn_id=m.turn_id,
                role=m.role,
                content=m.content,
                order_index=m.order_index,
                usage_json=m.usage_json,
                latency_ms=m.latency_ms,
                ttft_ms=m.ttft_ms,
                cost_usd=m.cost_usd,
                error=m.error,
                created_at=m.created_at,
                tool_calls=[ToolCallOut.model_validate(tc) for tc in m.tool_calls],
            )
            for m in messages
        ],
    )


def _build_user_message(db: DbSession, turn: Turn) -> ChatMessage:
    image_parts: list[dict] = []
    for att in turn.attachments:
        if att.kind != "image":
            continue
        path = os.path.join(settings.UPLOAD_DIR, att.storage_path)
        if os.path.exists(path):
            with open(path, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode()
            image_parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{att.mime_type};base64,{b64}"},
                }
            )
    text = turn.content + document_prompt_block(turn.attachments)
    if not image_parts:
        return {"role": "user", "content": text}
    parts: list[dict] = []
    if text:
        parts.append({"type": "text", "text": text})
    parts.extend(image_parts)
    return {"role": "user", "content": parts}


# ---------------- sessions CRUD ----------------


@router.get("", response_model=list[SessionListItem])
def list_sessions(
    user: User = Depends(current_user), db: DbSession = Depends(get_db)
) -> list[SessionListItem]:
    rows = db.scalars(
        select(ChatSession)
        .where(ChatSession.user_id == user.id)
        .order_by(ChatSession.updated_at.desc())
    ).all()
    out = []
    for s in rows:
        count = db.scalar(
            select(func.count(Lane.id)).where(Lane.session_id == s.id)
        )
        msg_count = db.scalar(
            select(func.count(Turn.id)).where(Turn.session_id == s.id)
        )
        out.append(
            SessionListItem(
                id=s.id,
                title=s.title,
                updated_at=s.updated_at,
                lane_count=count or 0,
                message_count=msg_count or 0,
                folder_id=s.folder_id,
                pinned=bool(s.pinned),
                archived=bool(s.archived),
                trashed=bool(s.trashed),
            )
        )
    return out


@router.get("/active")
def active_sessions(
    user: User = Depends(current_user), db: DbSession = Depends(get_db)
) -> dict:
    """Session ids (owned by the caller) that currently have a lane generating, so the
    sidebar can show a live spinner on those chats."""
    from ..broadcast import active_session_ids

    ids = active_session_ids()
    if not ids:
        return {"session_ids": []}
    owned = db.scalars(
        select(ChatSession.id).where(
            ChatSession.user_id == user.id, ChatSession.id.in_(ids)
        )
    ).all()
    return {"session_ids": list(owned)}


@router.get("/search", response_model=list[SearchHit])
def search_sessions(
    q: str,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> list[SearchHit]:
    term = (q or "").strip()
    if not term:
        return []
    like = f"%{term}%"
    # sessions whose title, a turn's content, or a lane message's content matches
    sess_ids: set[str] = set()
    for s in db.scalars(
        select(ChatSession).where(
            ChatSession.user_id == user.id, ChatSession.title.ilike(like)
        )
    ):
        sess_ids.add(s.id)
    for t in db.scalars(
        select(Turn)
        .join(ChatSession, ChatSession.id == Turn.session_id)
        .where(ChatSession.user_id == user.id, Turn.content.ilike(like))
    ):
        sess_ids.add(t.session_id)
    for m in db.scalars(
        select(LaneMessage)
        .join(Lane, Lane.id == LaneMessage.lane_id)
        .join(ChatSession, ChatSession.id == Lane.session_id)
        .where(ChatSession.user_id == user.id, LaneMessage.content.ilike(like))
    ):
        lane = db.get(Lane, m.lane_id)
        if lane:
            sess_ids.add(lane.session_id)

    hits: list[SearchHit] = []
    for sid in sess_ids:
        s = db.get(ChatSession, sid)
        if not s:
            continue
        # build a small snippet from the first matching turn
        snippet = ""
        for t in sorted(s.turns, key=lambda x: x.order_index):
            if term.lower() in (t.content or "").lower():
                snippet = t.content
                break
        if not snippet and term.lower() in (s.title or "").lower():
            snippet = s.title
        idx = snippet.lower().find(term.lower())
        if idx > 40:
            snippet = "…" + snippet[idx - 30 :]
        hits.append(
            SearchHit(
                session_id=s.id,
                title=s.title,
                snippet=snippet[:160],
                updated_at=s.updated_at,
            )
        )
    hits.sort(key=lambda h: h.updated_at, reverse=True)
    return hits


@router.post("", response_model=SessionDetail)
def create_session(
    payload: SessionCreate,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> SessionDetail:
    s = ChatSession(
        user_id=user.id,
        title=payload.title or "New topic",
        system_prompt=payload.system_prompt,
        tools_enabled=payload.tools_enabled,
        tool_config_json=payload.tool_config or {},
    )
    db.add(s)
    db.flush()
    for i, lane in enumerate(payload.lanes):
        _verify_provider(db, user, lane.provider_id)
        db.add(
            Lane(
                session_id=s.id,
                provider_id=lane.provider_id,
                model=lane.model,
                position=i,
                role=lane.role,
            )
        )
    db.commit()
    db.refresh(s)
    return _serialize_detail(db, s)


@router.get("/{session_id}", response_model=SessionDetail)
def get_session(
    session_id: str,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> SessionDetail:
    s = _get_session(db, user, session_id)
    return _serialize_detail(db, s)


def _clone_session(
    db: DbSession,
    user: User,
    src: ChatSession,
    *,
    up_to_order: int | None,
    single_lane_id: str | None,
    title: str,
) -> ChatSession:
    """Copy a session (optionally a single lane, optionally truncated at a turn order)
    into a brand-new session. Attachment rows point at the same files on disk."""
    new = ChatSession(
        user_id=user.id,
        title=title,
        system_prompt=src.system_prompt,
        tools_enabled=src.tools_enabled,
        tool_config_json=dict(src.tool_config_json or {}),
        folder_id=src.folder_id,
    )
    db.add(new)
    db.flush()

    src_lanes = sorted(
        [
            l
            for l in src.lanes
            if single_lane_id is None or l.id == single_lane_id
        ],
        key=lambda x: x.position,
    )
    lane_map: dict[str, str] = {}
    for i, l in enumerate(src_lanes):
        nl = Lane(
            session_id=new.id,
            provider_id=l.provider_id,
            model=l.model,
            position=i,
            role=l.role,
            hidden=l.hidden,
        )
        db.add(nl)
        db.flush()
        lane_map[l.id] = nl.id

    src_turns = sorted(
        [
            t
            for t in src.turns
            if up_to_order is None or t.order_index <= up_to_order
        ],
        key=lambda x: x.order_index,
    )
    turn_map: dict[str, str] = {}
    for t in src_turns:
        tgt = t.target_lane_ids_json
        if tgt:
            tgt = [lane_map[x] for x in tgt if x in lane_map] or None
        nt = Turn(
            session_id=new.id,
            order_index=t.order_index,
            content=t.content,
            target_lane_ids_json=tgt,
        )
        db.add(nt)
        db.flush()
        turn_map[t.id] = nt.id
        for a in t.attachments:
            db.add(
                Attachment(
                    turn_id=nt.id,
                    user_id=user.id,
                    kind=a.kind,
                    filename=a.filename,
                    mime_type=a.mime_type,
                    size_bytes=a.size_bytes,
                    storage_path=a.storage_path,
                    extracted_text=a.extracted_text,
                )
            )

    msgs = db.scalars(
        select(LaneMessage)
        .join(Lane, Lane.id == LaneMessage.lane_id)
        .where(Lane.session_id == src.id)
    ).all()
    for m in msgs:
        if m.lane_id in lane_map and m.turn_id in turn_map:
            db.add(
                LaneMessage(
                    lane_id=lane_map[m.lane_id],
                    turn_id=turn_map[m.turn_id],
                    role=m.role,
                    content=m.content,
                    order_index=m.order_index,
                    usage_json=m.usage_json,
                    latency_ms=m.latency_ms,
                    cost_usd=m.cost_usd,
                    error=m.error,
                )
            )
    db.commit()
    db.refresh(new)
    return new


@router.post("/{session_id}/branch", response_model=SessionDetail)
def branch_session(
    session_id: str,
    turn_id: str,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> SessionDetail:
    """Fork a session into a new one containing every lane and all turns up to (and
    including) the given turn — so you can explore alternatives without losing history."""
    s = _get_session(db, user, session_id)
    turn = db.get(Turn, turn_id)
    if not turn or turn.session_id != s.id:
        raise HTTPException(status_code=404, detail="Turn not found")
    new = _clone_session(
        db,
        user,
        s,
        up_to_order=turn.order_index,
        single_lane_id=None,
        title=f"{s.title} (branch)",
    )
    return _serialize_detail(db, new)


@router.post("/{session_id}/lanes/{lane_id}/continue", response_model=SessionDetail)
def continue_in_lane(
    session_id: str,
    lane_id: str,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> SessionDetail:
    """Promote a single lane into a new, focused single-model session that carries over
    that lane's full conversation history."""
    s = _get_session(db, user, session_id)
    lane = _get_lane(db, s, lane_id)
    new = _clone_session(
        db,
        user,
        s,
        up_to_order=None,
        single_lane_id=lane.id,
        title=f"{lane.model} — continued",
    )
    return _serialize_detail(db, new)


@router.get("/{session_id}/runs")
def session_runs(
    session_id: str,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> dict:
    """Return the lane ids currently generating for this session, so the client can
    reconcile lanes left stuck as 'streaming' by a dropped connection or reload."""
    from ..broadcast import active_lane_ids

    s = _get_session(db, user, session_id)
    return {"running": active_lane_ids(s.id)}


@router.get("/{session_id}/progress")
def session_progress(
    session_id: str,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> dict:
    """Partial text of lanes currently generating in the background (running with no SSE
    stream of their own), so a client that returned to the chat can show the answer as it
    grows instead of a blank spinner."""
    from ..broadcast import lane_progress

    s = _get_session(db, user, session_id)
    return {"lanes": lane_progress(s.id)}


@router.post("/{session_id}/resume")
async def session_resume(
    session_id: str,
    turn_id: str = Body(..., embed=True),
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> StreamingResponse:
    """Re-attach to an in-flight broadcast turn: replays the SSE events emitted so far,
    then tails new ones live until the run finishes. Lets a client that navigated away and
    returned stream the answer token-by-token instead of polling partial text. If no live
    run exists for the turn (already finished + cleaned up, or server restarted) the stream
    is empty and the client falls back to the persisted result / progress poll."""
    from ..broadcast import resume_stream

    s = _get_session(db, user, session_id)
    sid = s.id

    async def event_stream():
        async for chunk in resume_stream(sid, turn_id):
            yield chunk

    return StreamingResponse(event_stream(), media_type="text/event-stream")



@router.patch("/{session_id}", response_model=SessionDetail)
def update_session(
    session_id: str,
    payload: SessionUpdate,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> SessionDetail:
    s = _get_session(db, user, session_id)
    if payload.title is not None:
        s.title = payload.title
    if payload.system_prompt is not None:
        s.system_prompt = payload.system_prompt
    if payload.tools_enabled is not None:
        s.tools_enabled = payload.tools_enabled
    if payload.tool_config is not None:
        s.tool_config_json = payload.tool_config
    if payload.folder_id is not None:
        s.folder_id = payload.folder_id or None
    if payload.pinned is not None:
        s.pinned = payload.pinned
    if payload.archived is not None:
        s.archived = payload.archived
    if payload.trashed is not None:
        s.trashed = payload.trashed
    db.commit()
    db.refresh(s)
    return _serialize_detail(db, s)


@router.post("/{session_id}/autotitle", response_model=AutoTitleOut)
async def autotitle(
    session_id: str,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> AutoTitleOut:
    """Generate a short, human-friendly title for the session from its conversation,
    using the user's default provider/model."""
    s = _get_session(db, user, session_id)
    turns = sorted(s.turns, key=lambda t: t.order_index)
    if not turns:
        return AutoTitleOut(title=s.title)

    # Build a compact transcript (first user turn + first assistant reply if available).
    first = turns[0]
    convo = f"User: {first.content.strip()[:800]}"
    reply = db.scalar(
        select(LaneMessage)
        .where(LaneMessage.turn_id == first.id, LaneMessage.role == "assistant")
        .order_by(LaneMessage.order_index)
    )
    if reply and reply.content:
        convo += f"\nAssistant: {reply.content.strip()[:600]}"

    prov = pick_default_provider(db, user.id)
    if prov is None:
        return AutoTitleOut(title=s.title)
    model = prov.default_model or ((prov.models_json or [""])[0] if prov.models_json else "")
    if not model:
        return AutoTitleOut(title=s.title)

    system = (
        "You generate very short chat titles. Given a conversation, reply with a concise "
        "3-6 word title that captures the topic. Use Title Case. Return ONLY the title — "
        "no quotes, no trailing punctuation, no preamble."
    )
    try:
        llm = await build_provider(prov, db, model)
        text = ""
        async for ev in llm.stream(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": f"Conversation:\n{convo}\n\nTitle:"},
            ],
            None,
        ):
            if ev.type == "token" and ev.text:
                text += ev.text
    except Exception:  # noqa: BLE001 - titling is best-effort; keep old title on failure
        return AutoTitleOut(title=s.title)

    title = " ".join(text.strip().strip("\"'`").split())
    title = title.rstrip(".").strip()
    if len(title) > 80:
        title = title[:80].rsplit(" ", 1)[0]
    if not title:
        return AutoTitleOut(title=s.title)

    s.title = title
    db.commit()
    return AutoTitleOut(title=title)


@router.post("/{session_id}/synthesize")
async def synthesize(
    session_id: str,
    payload: dict,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> dict:
    """Merge the responder lanes' answers for a given turn into one best combined
    answer, using the user's default provider/model."""
    s = _get_session(db, user, session_id)
    turn_id = payload.get("turn_id")
    turn = db.get(Turn, turn_id) if turn_id else None
    if turn is None or turn.session_id != s.id:
        # Fall back to the latest turn.
        turns = sorted(s.turns, key=lambda t: t.order_index)
        turn = turns[-1] if turns else None
    if turn is None:
        raise HTTPException(status_code=400, detail="No turn to synthesize")

    lane_by_id = {l.id: l for l in s.lanes}
    msgs = db.scalars(
        select(LaneMessage)
        .where(LaneMessage.turn_id == turn.id, LaneMessage.role == "assistant")
        .order_by(LaneMessage.order_index)
    ).all()
    answers = []
    for m in msgs:
        lane = lane_by_id.get(m.lane_id)
        if lane and lane.role == "responder" and m.content and not m.error:
            answers.append((lane.model, m.content.strip()))
    if not answers:
        raise HTTPException(status_code=400, detail="No answers to synthesize yet")

    prov = pick_default_provider(db, user.id)
    if prov is None:
        raise HTTPException(status_code=400, detail="No AI provider configured")
    model = prov.default_model or ((prov.models_json or [""])[0] if prov.models_json else "")
    if not model:
        raise HTTPException(status_code=400, detail="Default provider has no model")

    system = (
        "You are an expert editor. You are given the SAME question answered by several "
        "different AI models. Produce ONE best, correct, well-structured combined answer: "
        "merge the strongest points, resolve contradictions, drop errors and repetition, "
        "and keep useful code/tables/diagrams. Do not mention the individual models."
    )
    parts = [f"QUESTION:\n{turn.content.strip()[:2000]}", ""]
    for model_name, content in answers:
        parts.append(f"--- Answer from {model_name} ---\n{content[:4000]}")
    parts.append("\nReturn ONLY the synthesized answer.")
    try:
        llm = await build_provider(prov, db, model)
        text = ""
        async for ev in llm.stream(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": "\n\n".join(parts)},
            ],
            None,
        ):
            if ev.type == "token" and ev.text:
                text += ev.text
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Synthesis failed: {exc}")

    text = text.strip()
    if not text:
        raise HTTPException(status_code=502, detail="The model returned an empty result.")
    return {
        "content": text,
        "used_provider": prov.name,
        "used_model": model,
        "source_count": len(answers),
    }


@router.delete("/trash/empty", status_code=204)
def empty_trash(
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
):
    """Permanently delete every trashed session for the current user in one call."""
    rows = db.scalars(
        select(ChatSession).where(
            ChatSession.user_id == user.id, ChatSession.trashed.is_(True)
        )
    ).all()
    for s in rows:
        db.delete(s)
    db.commit()


@router.delete("/{session_id}", status_code=204)
def delete_session(
    session_id: str,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
):
    s = _get_session(db, user, session_id)
    db.delete(s)
    db.commit()


@router.delete("/{session_id}/turns/{turn_id}", status_code=204)
def delete_turn(
    session_id: str,
    turn_id: str,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
):
    """Delete a turn (and its lane messages) — used by edit-and-resend."""
    s = _get_session(db, user, session_id)
    turn = db.get(Turn, turn_id)
    if not turn or turn.session_id != s.id:
        raise HTTPException(status_code=404, detail="Turn not found")
    db.delete(turn)
    db.commit()


# ---------------- lanes CRUD ----------------


@router.post("/{session_id}/lanes", response_model=LaneOut)
def add_lane(
    session_id: str,
    payload: LaneCreate,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> LaneOut:
    s = _get_session(db, user, session_id)
    if len(s.lanes) >= 6:
        raise HTTPException(status_code=400, detail="Maximum 6 lanes per session")
    _verify_provider(db, user, payload.provider_id)
    position = max((l.position for l in s.lanes), default=-1) + 1
    lane = Lane(
        session_id=s.id,
        provider_id=payload.provider_id,
        model=payload.model,
        position=position,
        role=payload.role,
    )
    db.add(lane)
    db.commit()
    db.refresh(lane)
    return LaneOut.model_validate(lane)


@router.patch("/{session_id}/lanes/{lane_id}", response_model=LaneOut)
def update_lane(
    session_id: str,
    lane_id: str,
    payload: LaneUpdate,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> LaneOut:
    s = _get_session(db, user, session_id)
    lane = _get_lane(db, s, lane_id)
    if payload.provider_id is not None:
        _verify_provider(db, user, payload.provider_id)
        lane.provider_id = payload.provider_id
    if payload.model is not None:
        lane.model = payload.model
    if payload.position is not None:
        lane.position = payload.position
    if payload.hidden is not None:
        lane.hidden = payload.hidden
    db.commit()
    db.refresh(lane)
    return LaneOut.model_validate(lane)


@router.delete("/{session_id}/lanes/{lane_id}", status_code=204)
def delete_lane(
    session_id: str,
    lane_id: str,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
):
    s = _get_session(db, user, session_id)
    lane = _get_lane(db, s, lane_id)
    db.delete(lane)
    db.commit()


# ---------------- broadcast (fan-out) ----------------


@router.post("/{session_id}/broadcast")
def broadcast(
    session_id: str,
    payload: BroadcastRequest,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> StreamingResponse:
    s = _get_session(db, user, session_id)
    responders = [l for l in s.lanes if l.role == "responder"]
    if not responders:
        raise HTTPException(status_code=400, detail="Session has no responder lanes")

    target_ids = payload.target_lane_ids
    if target_ids:
        targets = [l for l in responders if l.id in target_ids]
    else:
        targets = responders
    if not targets:
        raise HTTPException(status_code=400, detail="No matching target lanes")

    max_order = db.scalar(
        select(func.max(Turn.order_index)).where(Turn.session_id == s.id)
    )
    next_order = 0 if max_order is None else max_order + 1
    turn = Turn(
        session_id=s.id,
        order_index=next_order,
        content=payload.content,
        target_lane_ids_json=target_ids if target_ids else None,
    )
    db.add(turn)
    db.flush()

    # link attachments
    if payload.attachment_ids:
        for att_id in payload.attachment_ids:
            att = db.get(Attachment, att_id)
            if att and att.user_id == user.id:
                att.turn_id = turn.id
                db.add(att)
    db.commit()
    db.refresh(turn)

    user_message = _build_user_message(db, turn)
    lane_pairs = [(l.id, user_message) for l in targets]
    turn_id = turn.id
    sid = s.id

    async def event_stream():
        async for chunk in multiplex(sid, turn_id, lane_pairs):
            yield chunk

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/{session_id}/export")
def export_comparison(
    session_id: str,
    fmt: str = "md",
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> dict:
    s = _get_session(db, user, session_id)
    if fmt not in ("md", "docx", "pdf"):
        raise HTTPException(status_code=400, detail="format must be md, docx or pdf")
    try:
        stored_name, download_name, mime = build_export_file(db, s, fmt)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Export failed: {exc}")
    path = os.path.join(settings.UPLOAD_DIR, "generated", stored_name)
    size = os.path.getsize(path) if os.path.exists(path) else 0
    db.add(
        GeneratedFile(
            user_id=user.id,
            session_id=s.id,
            stored_name=stored_name,
            download_name=download_name,
            mime_type=mime,
            size_bytes=size,
            kind=fmt,
        )
    )
    db.commit()
    return {"url": f"/api/files/{stored_name}?name={download_name}", "download_name": download_name}


@router.post("/{session_id}/lanes/{lane_id}/regenerate")
def regenerate(
    session_id: str,
    lane_id: str,
    payload: RegenerateRequest,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> StreamingResponse:
    s = _get_session(db, user, session_id)
    lane = _get_lane(db, s, lane_id)
    if payload.turn_id:
        turn = db.get(Turn, payload.turn_id)
    else:
        turn = db.scalar(
            select(Turn)
            .where(Turn.session_id == s.id)
            .order_by(Turn.order_index.desc())
        )
    if not turn or turn.session_id != s.id:
        raise HTTPException(status_code=404, detail="Turn not found")

    # delete existing lane message for this turn
    existing = db.scalars(
        select(LaneMessage).where(
            LaneMessage.lane_id == lane.id, LaneMessage.turn_id == turn.id
        )
    ).all()
    for m in existing:
        db.delete(m)
    db.commit()

    user_message = _build_user_message(db, turn)
    turn_id = turn.id
    sid = s.id
    lid = lane.id

    async def event_stream():
        async for chunk in multiplex(sid, turn_id, [(lid, user_message)]):
            yield chunk

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/{session_id}/lanes/{lane_id}/stop", status_code=204)
def stop_lane(
    session_id: str,
    lane_id: str,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
):
    s = _get_session(db, user, session_id)
    lane = _get_lane(db, s, lane_id)
    request_stop(s.id, lane.id)


@router.post("/{session_id}/judge")
def judge(
    session_id: str,
    payload: JudgeRequest,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> StreamingResponse:
    s = _get_session(db, user, session_id)
    judge_lane = next((l for l in s.lanes if l.role == "judge"), None)
    if not judge_lane:
        raise HTTPException(status_code=400, detail="No judge lane configured")
    turn = db.get(Turn, payload.turn_id)
    if not turn or turn.session_id != s.id:
        raise HTTPException(status_code=404, detail="Turn not found")

    responders = [l for l in s.lanes if l.role == "responder"]
    answers = []
    for lane in responders:
        provider = db.get(Provider, lane.provider_id)
        msg = db.scalar(
            select(LaneMessage).where(
                LaneMessage.lane_id == lane.id,
                LaneMessage.turn_id == turn.id,
                LaneMessage.role == "assistant",
            )
        )
        label = f"{provider.name if provider else 'model'} / {lane.model}"
        answers.append(f"### Answer from {label}\n{msg.content if msg else '(no answer)'}")

    prompt = (
        "You are a judge. The user asked:\n\n"
        f"{turn.content}\n\n"
        "Several AI models answered below. Synthesize the single best, most accurate "
        "answer, noting where they agree or differ.\n\n" + "\n\n".join(answers)
    )
    user_message: ChatMessage = {"role": "user", "content": prompt}

    # remove prior judge message for this turn
    for m in db.scalars(
        select(LaneMessage).where(
            LaneMessage.lane_id == judge_lane.id, LaneMessage.turn_id == turn.id
        )
    ).all():
        db.delete(m)
    db.commit()

    turn_id = turn.id
    sid = s.id
    lid = judge_lane.id

    async def event_stream():
        async for chunk in multiplex(sid, turn_id, [(lid, user_message)]):
            yield chunk

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------------- export / import ----------------


@router.get("/{session_id}/export")
def export_session(
    session_id: str,
    format: str = "json",
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> StreamingResponse:
    s = _get_session(db, user, session_id)
    detail = _serialize_detail(db, s)
    if format == "md":
        lines = [f"# {s.title}\n"]
        if s.system_prompt:
            lines.append(f"**System prompt:** {s.system_prompt}\n")
        lane_by_id = {l.id: l for l in detail.lanes}
        for turn in detail.turns:
            lines.append(f"\n## Prompt\n{turn.content}\n")
            for m in detail.messages:
                if m.turn_id != turn.id or m.role != "assistant":
                    continue
                lane = lane_by_id.get(m.lane_id)
                header = f"{lane.model}" if lane else m.lane_id
                lines.append(f"\n### {header}\n{m.content}\n")
                for tc in m.tool_calls:
                    lines.append(
                        f"\n> tool `{tc.tool_name}` "
                        f"({json.dumps(tc.arguments_json)})\n"
                    )
        body = "\n".join(lines)
        return StreamingResponse(
            iter([body]),
            media_type="text/markdown",
            headers={
                "Content-Disposition": f'attachment; filename="{s.title}.md"'
            },
        )
    body = detail.model_dump_json(indent=2)
    return StreamingResponse(
        iter([body]),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{s.title}.json"'},
    )


@router.post("/import", response_model=SessionDetail)
def import_session(
    payload: dict,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> SessionDetail:
    s = ChatSession(
        user_id=user.id,
        title=payload.get("title", "Imported topic"),
        system_prompt=payload.get("system_prompt"),
        tools_enabled=payload.get("tools_enabled", False),
        tool_config_json=payload.get("tool_config_json", {}),
    )
    db.add(s)
    db.flush()

    # recreate lanes (map old lane id -> new lane), verifying provider ownership
    old_to_new: dict[str, str] = {}
    for i, lane in enumerate(payload.get("lanes", [])):
        provider = db.get(Provider, lane.get("provider_id"))
        if not provider or provider.user_id != user.id:
            continue
        new_lane = Lane(
            session_id=s.id,
            provider_id=lane["provider_id"],
            model=lane["model"],
            position=lane.get("position", i),
            role=lane.get("role", "responder"),
        )
        db.add(new_lane)
        db.flush()
        old_to_new[lane["id"]] = new_lane.id

    old_turn_to_new: dict[str, str] = {}
    for turn in payload.get("turns", []):
        new_turn = Turn(
            session_id=s.id,
            order_index=turn.get("order_index", 0),
            content=turn.get("content", ""),
            target_lane_ids_json=None,
        )
        db.add(new_turn)
        db.flush()
        old_turn_to_new[turn["id"]] = new_turn.id

    for m in payload.get("messages", []):
        lane_id = old_to_new.get(m.get("lane_id"))
        turn_id = old_turn_to_new.get(m.get("turn_id"))
        if not lane_id or not turn_id:
            continue
        db.add(
            LaneMessage(
                lane_id=lane_id,
                turn_id=turn_id,
                role=m.get("role", "assistant"),
                content=m.get("content", ""),
                order_index=m.get("order_index", 0),
                usage_json=m.get("usage_json"),
                latency_ms=m.get("latency_ms"),
                cost_usd=m.get("cost_usd"),
            )
        )
    db.commit()
    db.refresh(s)
    return _serialize_detail(db, s)
