from __future__ import annotations

import base64
import hashlib
import json
import os
import unicodedata
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import event, func, or_, select, update
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import object_session, selectinload

from ..broadcast import active_lane_ids, multiplex, request_stop
from ..config import settings
from ..db import get_db
from ..documents import document_prompt_block
from ..export import export_message_pdf
from ..export import export_session as build_export_file
from ..models import (
    Attachment,
    DeliberationRun,
    Folder,
    GeneratedFile,
    Lane,
    LaneMessage,
    Provider,
    ToolCall,
    Turn,
    User,
)
from ..models import (
    Session as ChatSession,
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
    MessageExportRequest,
    RegenerateRequest,
    SearchHit,
    SessionCreate,
    SessionDetail,
    SessionListItem,
    SessionUpdate,
    ToolCallOut,
    TurnOut,
)
from ..security import current_user

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


# ---------------- helpers ----------------


@event.listens_for(ChatSession, "after_insert")
def _remember_new_detail_session(_mapper, _connection, target) -> None:
    db = object_session(target)
    if db is not None:
        db.info.setdefault("_new_detail_sessions", set()).add(target.id)


@event.listens_for(DbSession, "after_transaction_end")
def _clear_new_detail_sessions(db, transaction) -> None:
    if transaction.parent is None:
        db.info.pop("_new_detail_sessions", None)


def _touch_detail_version(_mapper, connection, target) -> None:
    """Invalidate detail in the SAME transaction as an ORM transcript write.

    Broadcast/deliberation workers use separate DB sessions, and child rows have no
    updated_at column. Persist a parent stamp rather than a process-local cache or a
    read-time hash of message/tool bodies. Read the old FK too: moving an attachment
    must invalidate both sessions, even if its previous turn wasn't loaded by the ORM.
    Public transcript writers use ORM flushes; database cascades removing lanes are
    additionally covered by the small lane snapshot in _detail_etag.
    """
    if isinstance(target, (Lane, Turn)):
        table = type(target)
        scope = or_(
            ChatSession.id == target.session_id,
            ChatSession.id.in_(select(table.session_id).where(table.id == target.id)),
        )
    else:
        if isinstance(target, LaneMessage):
            parent, foreign_key = Lane.id, LaneMessage.lane_id
            owners = select(Lane.session_id)
        elif isinstance(target, ToolCall):
            parent, foreign_key = LaneMessage.id, ToolCall.lane_message_id
            owners = select(Lane.session_id).join(LaneMessage, LaneMessage.lane_id == Lane.id)
        else:  # Attachment
            parent, foreign_key = Turn.id, Attachment.turn_id
            owners = select(Turn.session_id)
        old_parent = select(foreign_key).where(type(target).id == target.id)
        owners = owners.where(or_(
            parent == getattr(target, foreign_key.key), parent.in_(old_parent),
        ))
        scope = ChatSession.id.in_(owners)
    db = object_session(target)
    new_ids = db.info.get("_new_detail_sessions", set()) if db is not None else set()
    if new_ids:
        # Building a new graph (including timestamp-preserving system restores) must
        # not overwrite its supplied history. The marker expires at commit/rollback;
        # later edits in this same ORM session still invalidate normally.
        scope = scope & ChatSession.id.not_in(new_ids)
    connection.execute(
        update(ChatSession).where(scope).values(updated_at=datetime.now(UTC))
    )


for _detail_model in (Lane, Turn, LaneMessage, ToolCall, Attachment):
    # Before update/delete retains the previous parent reference; after insert also
    # handles foreign keys populated by relationship assignment during the flush.
    for _detail_event in ("after_insert", "before_update", "before_delete"):
        event.listen(_detail_model, _detail_event, _touch_detail_version)


class _SessionCreate(SessionCreate):
    # Keep the request extension local to this router; no persistence schema change.
    folder_id: str | None = None


def _verify_folder(db: DbSession, user: User, folder_id: str | None) -> str | None:
    if not folder_id:
        return None
    folder = db.get(Folder, folder_id)
    if not folder or folder.user_id != user.id:
        raise HTTPException(status_code=404, detail="Folder not found")
    return folder.id


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


def _tool_call_out(tc: ToolCall, *, full_result: bool = False) -> ToolCallOut:
    """Serialize a tool call, shortening the result body for the session payload.

    Tool results can be tens of KB each and a busy session holds hundreds of them, but the
    UI only shows a preview until a card is expanded — so the full body is fetched on
    demand from ``/tool-calls/{id}`` instead of being shipped with every transcript.
    """
    result = tc.result_json
    truncated = False
    if not full_result and isinstance(result, dict):
        body = result.get("result")
        if isinstance(body, str) and len(body) > settings.TOOL_RESULT_PREVIEW_CHARS:
            result = {
                **result,
                "result": body[: settings.TOOL_RESULT_PREVIEW_CHARS],
            }
            truncated = True
    return ToolCallOut(
        id=tc.id,
        tool_name=tc.tool_name,
        arguments_json=tc.arguments_json,
        result_json=result,
        citations_json=tc.citations_json,
        status=tc.status,
        created_at=tc.created_at,
        result_truncated=truncated,
    )


def _serialize_detail(
    db: DbSession, s: ChatSession, *, full_tool_results: bool = False
) -> SessionDetail:
    # Explicit eager loads: `s.lanes` / `s.turns` and each turn's attachments would
    # otherwise lazy-load, costing one query per turn on a long transcript.
    lanes = db.scalars(
        select(Lane).where(Lane.session_id == s.id).order_by(Lane.position)
    ).all()
    turns = db.scalars(
        select(Turn)
        .where(Turn.session_id == s.id)
        .options(selectinload(Turn.attachments))
        .order_by(Turn.order_index)
    ).all()
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
        notice=s.notice,
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
                tool_calls=[
                    _tool_call_out(tc, full_result=full_tool_results) for tc in m.tool_calls
                ],
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
    # Deliberations are sessions too, and they live in the same list as chats — pinning,
    # renaming, archiving and trashing are session-level features, so keeping them out of
    # this list was the only reason they lacked all of it.
    rows = db.scalars(
        select(ChatSession)
        .where(ChatSession.user_id == user.id)
        .order_by(ChatSession.updated_at.desc())
    ).all()
    # Counts come from two grouped queries rather than two per row: this endpoint is
    # polled while a panel runs, and at 60 sessions the per-row version was 122 queries.
    lane_counts = dict(
        db.execute(
            select(Lane.session_id, func.count(Lane.id))
            .join(ChatSession, ChatSession.id == Lane.session_id)
            .where(ChatSession.user_id == user.id)
            .group_by(Lane.session_id)
        ).all()
    )
    turn_counts = dict(
        db.execute(
            select(Turn.session_id, func.count(Turn.id))
            .join(ChatSession, ChatSession.id == Turn.session_id)
            .where(ChatSession.user_id == user.id)
            .group_by(Turn.session_id)
        ).all()
    )
    # Latest run per deliberation session, fetched in one pass rather than per row.
    runs: dict[str, DeliberationRun] = {}
    for run in db.scalars(
        select(DeliberationRun)
        .where(DeliberationRun.user_id == user.id)
        .order_by(DeliberationRun.created_at)
    ).all():
        runs[run.session_id] = run
    out = []
    for s in rows:
        run = runs.get(s.id) if s.mode == "deliberation" else None
        out.append(
            SessionListItem(
                id=s.id,
                title=s.title,
                updated_at=s.updated_at,
                lane_count=lane_counts.get(s.id, 0),
                message_count=turn_counts.get(s.id, 0),
                folder_id=s.folder_id,
                pinned=bool(s.pinned),
                archived=bool(s.archived),
                trashed=bool(s.trashed),
                mode=s.mode or "compare",
                run_id=run.id if run else None,
                status=run.status if run else None,
                converged=bool(run.converged) if run else None,
                total_calls=run.total_calls if run else None,
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
    lowered = term.lower()

    # Matching session ids from titles, turn content and lane-message content in one
    # round trip. The message branch joins straight through to the session; resolving
    # each hit's lane with a separate `db.get` used to make this O(matches) queries.
    title_q = select(ChatSession.id).where(
        ChatSession.user_id == user.id, ChatSession.title.ilike(like)
    )
    turn_q = (
        select(Turn.session_id)
        .join(ChatSession, ChatSession.id == Turn.session_id)
        .where(ChatSession.user_id == user.id, Turn.content.ilike(like))
    )
    message_q = (
        select(Lane.session_id)
        .join(LaneMessage, LaneMessage.lane_id == Lane.id)
        .join(ChatSession, ChatSession.id == Lane.session_id)
        .where(ChatSession.user_id == user.id, LaneMessage.content.ilike(like))
    )
    sess_ids = set(db.scalars(title_q.union(turn_q, message_q)).all())
    if not sess_ids:
        return []

    rows = db.execute(
        select(ChatSession.id, ChatSession.title, ChatSession.updated_at).where(
            ChatSession.id.in_(sess_ids)
        )
    ).all()

    # Earliest matching turn per session, instead of loading every turn of every hit and
    # scanning them in Python.
    snippets: dict[str, str] = {}
    for session_id, content in db.execute(
        select(Turn.session_id, Turn.content)
        .where(Turn.session_id.in_(sess_ids), Turn.content.ilike(like))
        .order_by(Turn.session_id, Turn.order_index)
    ).all():
        snippets.setdefault(session_id, content or "")

    hits: list[SearchHit] = []
    for sid, title, updated_at in rows:
        snippet = snippets.get(sid) or ""
        if not snippet and lowered in (title or "").lower():
            snippet = title
        idx = snippet.lower().find(lowered)
        if idx > 40:
            snippet = "…" + snippet[idx - 30 :]
        hits.append(
            SearchHit(
                session_id=sid,
                title=title,
                snippet=snippet[:160],
                updated_at=updated_at,
            )
        )
    hits.sort(key=lambda h: h.updated_at, reverse=True)
    return hits


@router.post("", response_model=SessionDetail)
def create_session(
    payload: _SessionCreate,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> SessionDetail:
    folder_id = _verify_folder(db, user, payload.folder_id)
    for lane in payload.lanes:
        _verify_provider(db, user, lane.provider_id)
    s = ChatSession(
        user_id=user.id,
        title=payload.title or "New topic",
        system_prompt=payload.system_prompt,
        notice=payload.notice,
        tools_enabled=payload.tools_enabled,
        tool_config_json=payload.tool_config or {},
        folder_id=folder_id,
    )
    db.add(s)
    db.flush()
    for i, lane in enumerate(payload.lanes):
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


def _detail_etag(db: DbSession, s: ChatSession) -> str:
    """Small metadata/aggregate reads, never transcript or tool-result bodies.

    The persisted write stamp covers in-place edits. The lane snapshot also catches
    provider-deletion FK cascades (which bypass ORM events), including empty lanes.
    Turn/message aggregates cover structural changes without loading their content.
    """
    stamp = db.execute(
        select(
            func.count(LaneMessage.id), func.max(LaneMessage.created_at),
            select(func.count(Turn.id)).where(Turn.session_id == s.id).scalar_subquery(),
            select(func.max(Turn.created_at)).where(Turn.session_id == s.id).scalar_subquery(),
        )
        .join(Lane, Lane.id == LaneMessage.lane_id)
        .where(Lane.session_id == s.id)
    ).one()
    lanes = db.execute(
        select(Lane.id, Lane.provider_id, Lane.model, Lane.position, Lane.role,
               Lane.state, Lane.hidden, Lane.created_at)
        .where(Lane.session_id == s.id).order_by(Lane.id)
    ).all()
    raw = f"{s.id}:{s.updated_at}:{tuple(stamp)!r}:{[tuple(lane) for lane in lanes]!r}"
    return '"' + hashlib.sha1(raw.encode()).hexdigest() + '"'


@router.get("/{session_id}", response_model=SessionDetail)
def get_session(
    session_id: str,
    request: Request,
    response: Response,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
):
    s = _get_session(db, user, session_id)
    etag = _detail_etag(db, s)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return _serialize_detail(db, s)


@router.get("/{session_id}/tool-calls/{tool_call_id}")
def get_tool_call(
    session_id: str,
    tool_call_id: str,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> dict:
    """The untruncated body of one tool call (the session payload ships a preview)."""
    s = _get_session(db, user, session_id)
    tc = db.get(ToolCall, tool_call_id)
    if not tc:
        raise HTTPException(status_code=404, detail="Tool call not found")
    owner = db.scalar(
        select(Lane.session_id)
        .join(LaneMessage, LaneMessage.lane_id == Lane.id)
        .where(LaneMessage.id == tc.lane_message_id)
    )
    if owner != s.id:
        raise HTTPException(status_code=404, detail="Tool call not found")
    return {
        "id": tc.id,
        "tool_name": tc.tool_name,
        "arguments_json": tc.arguments_json,
        "result_json": tc.result_json,
        "citations_json": tc.citations_json,
        "status": tc.status,
    }


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
        notice=src.notice,
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
    # Validate ownership before changing ANY fields (including a simultaneous rename).
    folder_id = _verify_folder(db, user, payload.folder_id)
    if payload.title is not None:
        s.title = payload.title
    if payload.system_prompt is not None:
        s.system_prompt = payload.system_prompt
    if payload.notice is not None:
        s.notice = payload.notice
    if payload.tools_enabled is not None:
        s.tools_enabled = payload.tools_enabled
    if payload.tool_config is not None:
        s.tool_config_json = payload.tool_config
    if payload.folder_id is not None:
        s.folder_id = folder_id
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
    row_ids = [s.id for s in rows]
    active_deliberation = (
        db.scalar(
            select(DeliberationRun.id).where(
                DeliberationRun.session_id.in_(row_ids),
                DeliberationRun.status.in_(("pending", "running")),
            )
        )
        if row_ids
        else None
    )
    if active_deliberation or any(active_lane_ids(s.id) for s in rows):
        raise HTTPException(
            status_code=409,
            detail="Stop active generations before emptying the trash",
        )
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
    active_run = db.scalar(
        select(DeliberationRun.id).where(
            DeliberationRun.session_id == s.id,
            DeliberationRun.status.in_(("pending", "running")),
        )
    )
    if active_lane_ids(s.id) or active_run:
        raise HTTPException(
            status_code=409,
            detail="Stop the active generation before permanently deleting this session",
        )
    # Remove run rows explicitly so ORM cascades clean up their steps consistently before
    # the session row and its transcript disappear.
    for run in db.scalars(
        select(DeliberationRun).where(DeliberationRun.session_id == s.id)
    ).all():
        db.delete(run)
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
    payload: MessageExportRequest | None = None,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> dict:
    s = _get_session(db, user, session_id)
    if fmt not in ("md", "docx", "pdf"):
        raise HTTPException(status_code=400, detail="format must be md, docx or pdf")
    try:
        # The UI hands over the mermaid diagrams it has already rendered; without them a
        # ```mermaid``` fence would land in the document as raw source.
        stored_name, download_name, mime = build_export_file(
            db, s, fmt, _decode_diagrams(payload)
        )
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


# A rasterized mermaid diagram is a few hundred KB; these caps stop a crafted request from
# pushing the server into a huge allocation.
_MAX_DIAGRAMS = 40
_MAX_DIAGRAM_BYTES = 8 * 1024 * 1024
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _decode_diagrams(payload: MessageExportRequest | None) -> list[dict]:
    """Decode client-supplied diagram PNGs, skipping anything malformed or oversized."""
    out: list[dict] = []
    for item in (payload.diagrams if payload else None) or []:
        if len(out) >= _MAX_DIAGRAMS:
            break
        ref = (item.image or "").strip()
        if not ref.startswith("data:image/png;base64,"):
            continue
        b64 = ref.split(",", 1)[1]
        if len(b64) > _MAX_DIAGRAM_BYTES:
            continue
        try:
            data = base64.b64decode(b64, validate=True)
        except Exception:  # noqa: BLE001 — ignore an undecodable diagram, keep the export
            continue
        if not data.startswith(_PNG_MAGIC):
            continue
        out.append(
            {
                "code": item.code or "",
                "data": data,
                "width": item.width or 0,
                "height": item.height or 0,
            }
        )
    return out


@router.post("/{session_id}/messages/{message_id}/export")
def export_single_message(
    session_id: str,
    message_id: str,
    payload: MessageExportRequest | None = None,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> dict:
    """Export a single lane answer as a standalone US Letter PDF."""
    s = _get_session(db, user, session_id)
    msg = db.scalars(
        select(LaneMessage)
        .join(Lane, Lane.id == LaneMessage.lane_id)
        .where(LaneMessage.id == message_id, Lane.session_id == s.id)
    ).first()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    if not (msg.content or "").strip():
        raise HTTPException(status_code=400, detail="This response has no content to export")

    try:
        stored_name, download_name, mime = export_message_pdf(
            db, s, msg, _decode_diagrams(payload)
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Export failed: {exc}")

    path = os.path.join(settings.UPLOAD_DIR, "generated", stored_name)
    db.add(
        GeneratedFile(
            user_id=user.id,
            session_id=s.id,
            stored_name=stored_name,
            download_name=download_name,
            mime_type=mime,
            size_bytes=os.path.getsize(path) if os.path.exists(path) else 0,
            kind="pdf",
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


def _export_disposition(title: str, extension: str) -> str:
    """RFC 6266/5987 filename: safe ASCII fallback plus percent-encoded UTF-8.

    A title is display text, not a path or header value. Strip controls (including
    bidi controls), replace path/Windows-special characters, and bound the name.
    """
    title = unicodedata.normalize("NFC", title)
    stem = "".join(
        "_" if c in '/\\:*?"<>|' or unicodedata.category(c).startswith("C") else c
        for c in title
    ).strip(" .")[:120]
    stem = stem.encode("utf-8")[:180].decode("utf-8", errors="ignore").rstrip(" .") or "session"
    fallback = unicodedata.normalize("NFKD", stem).encode("ascii", errors="ignore").decode()
    # Compatibility normalization can turn full-width quotes/slashes into ASCII.
    fallback = fallback.translate(str.maketrans({c: "_" for c in '/\\:*?"<>|'}))
    fallback = fallback.strip(" .") or "session"
    encoded = quote(f"{stem}.{extension}", safe="")
    return f'attachment; filename="{fallback}.{extension}"; filename*=UTF-8\'\'{encoded}'


@router.get("/{session_id}/export")
def export_session(
    session_id: str,
    format: str = "json",
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> StreamingResponse:
    s = _get_session(db, user, session_id)
    # A download is an archive, not a UI preview: retain full tool result bodies.
    detail = _serialize_detail(db, s, full_tool_results=True)
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
                "Content-Disposition": _export_disposition(s.title, "md")
            },
        )
    body = detail.model_dump_json(indent=2)
    return StreamingResponse(
        iter([body]),
        media_type="application/json",
        headers={"Content-Disposition": _export_disposition(s.title, "json")},
    )


class _ImportRecord(BaseModel):
    # Do not coerce malformed containers, numbers, or booleans into transcript fields.
    # Unknown export metadata is ignored for compatibility, never used as a DB FK.
    model_config = ConfigDict(strict=True)
    created_at: datetime | None = Field(default=None, strict=False)


class _ImportLane(_ImportRecord):
    id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    model: str
    position: int | None = None
    role: Literal["responder", "judge"] = "responder"
    state: str = "idle"
    hidden: bool = False


class _ImportTurn(_ImportRecord):
    id: str = Field(min_length=1)
    order_index: int = 0
    content: str = ""
    target_lane_ids_json: list[str] | None = None
    # Metadata only. Never attach existing files by untrusted imported ids or paths.
    attachments: list[dict] = Field(default_factory=list)


class _ImportToolCall(_ImportRecord):
    tool_name: str
    arguments_json: dict = Field(default_factory=dict)
    result_json: dict | None = None
    citations_json: list | None = None
    status: str = "running"


class _ImportMessage(_ImportRecord):
    lane_id: str
    turn_id: str
    role: str = "assistant"
    content: str = ""
    order_index: int = 0
    usage_json: dict | None = None
    latency_ms: int | None = None
    ttft_ms: int | None = None
    cost_usd: float | None = Field(default=None, allow_inf_nan=False)
    error: str | None = None
    tool_calls: list[_ImportToolCall] = Field(default_factory=list)


class _SessionImport(_ImportRecord):
    title: str = "Imported topic"
    system_prompt: str | None = None
    notice: str | None = None
    tools_enabled: bool = False
    tool_config_json: dict = Field(default_factory=dict)
    folder_id: str | None = None
    pinned: bool = False
    archived: bool = False
    updated_at: datetime | None = Field(default=None, strict=False)
    lanes: list[_ImportLane] = Field(default_factory=list)
    turns: list[_ImportTurn] = Field(default_factory=list)
    messages: list[_ImportMessage] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def validate_json_encoding(cls, value):
        # Arbitrary nested tool/config JSON may contain escaped lone surrogates or
        # non-finite numbers. Reject before inserts AND before FastAPI could echo an
        # unencodable value in its validation-error response.
        try:
            json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422, detail="Import must contain finite, UTF-8 JSON data"
            ) from exc
        return value

    @model_validator(mode="after")
    def validate_references(self):
        lane_ids = {lane.id for lane in self.lanes}
        turn_ids = {turn.id for turn in self.turns}
        if len(lane_ids) != len(self.lanes) or len(turn_ids) != len(self.turns):
            raise ValueError("Lane and turn ids must be unique within the import")
        for message in self.messages:
            if message.lane_id not in lane_ids or message.turn_id not in turn_ids:
                raise ValueError("Messages must refer to lanes and turns in the import")
        return self


def _import_fields(record: _ImportRecord, **kwargs) -> dict:
    data = record.model_dump(**kwargs)
    if data.get("created_at") is None:
        data.pop("created_at", None)
    return data


@router.post("/import", response_model=SessionDetail)
def import_session(
    payload: _SessionImport,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> SessionDetail:
    # FastAPI validates the entire graph before this handler can perform any writes.
    folder_id = _verify_folder(db, user, payload.folder_id)
    provider_ids = {lane.provider_id for lane in payload.lanes}
    owned_providers = set(db.scalars(
        select(Provider.id).where(Provider.user_id == user.id, Provider.id.in_(provider_ids))
    ).all()) if provider_ids else set()
    s = ChatSession(
        user_id=user.id,
        title=payload.title,
        system_prompt=payload.system_prompt,
        notice=payload.notice,
        tools_enabled=payload.tools_enabled,
        tool_config_json=payload.tool_config_json,
        folder_id=folder_id,
        pinned=payload.pinned,
        archived=payload.archived,
    )
    try:
        db.add(s)
        db.flush()

        # Missing/foreign providers intentionally skip their lanes and messages. Every
        # surviving reference is mapped to a newly created row, never an external id.
        old_to_new: dict[str, str] = {}
        for i, lane in enumerate(payload.lanes):
            if lane.provider_id not in owned_providers:
                continue
            new_lane = Lane(
                session_id=s.id,
                **_import_fields(lane, exclude={"id", "position"}),
                position=lane.position if lane.position is not None else i,
            )
            db.add(new_lane)
            db.flush()
            old_to_new[lane.id] = new_lane.id

        target_map = dict(old_to_new)
        old_turn_to_new: dict[str, str] = {}
        for turn in payload.turns:
            targets = turn.target_lane_ids_json
            if targets:
                for target in targets:
                    # Deleted lanes leave historical targets in legitimate exports.
                    # Remap missing/skipped targets to inert, fresh IDs rather than
                    # []/None, which history reconstruction treats as "all lanes".
                    if target not in target_map:
                        target_map[target] = str(uuid4())
            new_turn = Turn(
                session_id=s.id,
                **_import_fields(turn, exclude={"id", "attachments", "target_lane_ids_json"}),
                target_lane_ids_json=(
                    [target_map[x] for x in targets]
                    if targets is not None else None
                ),
            )
            db.add(new_turn)
            db.flush()
            old_turn_to_new[turn.id] = new_turn.id

        for message in payload.messages:
            lane_id = old_to_new.get(message.lane_id)
            if lane_id is None:
                continue
            new_message = LaneMessage(
                lane_id=lane_id,
                turn_id=old_turn_to_new[message.turn_id],
                **_import_fields(message, exclude={"lane_id", "turn_id", "tool_calls"}),
            )
            db.add(new_message)
            db.flush()
            for call in message.tool_calls:
                db.add(ToolCall(lane_message_id=new_message.id, **_import_fields(call)))

        # Flush the complete graph before restoring archive timestamps. New session
        # graphs are excluded from child invalidation until this transaction ends.
        db.flush()
        db.refresh(s)
        if payload.created_at is not None:
            s.created_at = payload.created_at
        if payload.updated_at is not None:
            s.updated_at = payload.updated_at
        db.flush()
        db.refresh(s)
        detail = _serialize_detail(db, s)
        db.commit()
        return detail
    except Exception:
        db.rollback()
        raise
