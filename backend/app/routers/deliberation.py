"""HTTP surface for Model Deliberation.

A deliberation is an ordinary ``Session`` with ``mode="deliberation"``: panelists are
``Lane`` rows and every round's answer is a ``LaneMessage``. That means transcript export,
search, artifacts and the per-message PDF all work on it without special-casing. The
``DeliberationRun`` row holds only what is unique to the protocol.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..benchmark import ARMS, run_benchmark
from ..config import settings
from ..db import get_db
from ..deliberation import is_running, request_stop, stream_run
from ..errors import log_and_describe
from ..models import (
    Attachment,
    DeliberationRun,
    DeliberationStep,
    Lane,
    Provider,
    Turn,
    User,
)
from ..models import (
    Session as ChatSession,
)
from ..providers.registry import build_provider, pick_default_provider
from ..schemas import MessageExportRequest
from ..security import current_user
from ..structured import extract_json

router = APIRouter(prefix="/api/deliberations", tags=["deliberation"])


class PanelMember(BaseModel):
    provider_id: str
    model: str


class DeliberationCreate(BaseModel):
    prompt: str
    title: str | None = None
    participants: list[PanelMember] = Field(default_factory=list)
    judge: PanelMember | None = None
    max_rounds: int = settings.DELIBERATION_DEFAULT_ROUNDS
    synthesis: bool = True
    minority_report: bool = True
    critique_synthesis: bool = True
    # "council" = full peer review; "quick" = draft + Borda vote (the cheap baseline)
    mode: str = "council"
    evidence: bool = False
    # Images uploaded via /api/uploads and shown to the panel alongside the question.
    attachment_ids: list[str] = Field(default_factory=list)


class ClassifyRequest(BaseModel):
    prompt: str


class FollowupRequest(BaseModel):
    """A follow-up question for a panel that has already answered once."""

    prompt: str
    attachment_ids: list[str] = Field(default_factory=list)


class ContinueRequest(BaseModel):
    """Which answer to carry into a normal chat (default: the synthesis)."""

    step_id: str | None = None


class BenchmarkRequest(BaseModel):
    """Head-to-head comparison of deliberation against the cheaper alternatives."""

    prompts: list[str] = Field(default_factory=list)
    participants: list[PanelMember] = Field(default_factory=list)
    judge: PanelMember | None = None
    max_rounds: int = settings.DELIBERATION_DEFAULT_ROUNDS
    arms: list[str] = Field(default_factory=lambda: list(ARMS))


def _get_run(db: DbSession, user: User, run_id: str) -> DeliberationRun:
    run = db.get(DeliberationRun, run_id)
    if not run or run.user_id != user.id:
        raise HTTPException(status_code=404, detail="Deliberation not found")
    return run


def _serialize(db: DbSession, run: DeliberationRun) -> dict:
    session = db.get(ChatSession, run.session_id)
    lanes = sorted(session.lanes, key=lambda l: l.position) if session else []
    providers = {p.id: p for p in db.scalars(select(Provider)).all()}
    turn = db.get(Turn, run.turn_id)
    steps = db.scalars(
        select(DeliberationStep)
        .where(DeliberationStep.run_id == run.id)
        .order_by(DeliberationStep.round_index, DeliberationStep.created_at)
    ).all()
    # Follow-ups live in the same session, so the whole conversation with this panel can
    # be walked from any run in it.
    thread = db.scalars(
        select(DeliberationRun)
        .where(DeliberationRun.session_id == run.session_id)
        .order_by(DeliberationRun.created_at)
    ).all()
    return {
        "id": run.id,
        "session_id": run.session_id,
        "turn_id": run.turn_id,
        "title": session.title if session else "",
        "status": run.status,
        "running": is_running(run.id),
        "prompt": run.prompt,
        "parent_run_id": (run.config_json or {}).get("parent_run_id"),
        "thread": [
            {
                "id": r.id,
                "prompt": r.prompt[:160],
                "status": r.status,
                "created_at": r.created_at.isoformat(),
            }
            for r in thread
        ],
        "images": [
            {"id": a.id, "filename": a.filename, "url": f"/api/uploads/{a.id}"}
            for a in (turn.attachments if turn else [])
            if a.kind == "image"
        ],
        "documents": [
            {
                "id": a.id,
                "filename": a.filename,
                "chars": len(a.extracted_text or ""),
            }
            for a in (turn.attachments if turn else [])
            if a.kind == "document"
        ],
        "rounds_used": run.rounds_used,
        "converged": run.converged,
        "config": run.config_json or {},
        "convergence": run.convergence_json or [],
        "vote": run.vote_json or {},
        "metrics": run.metrics_json or {},
        "synthesis": run.synthesis,
        "minority_report": run.minority_report,
        "extraction": run.extraction_json or {},
        "synthesis_critique": run.synthesis_critique_json or {},
        "total_calls": run.total_calls,
        "wall_ms": run.wall_ms,
        "error": run.error,
        "created_at": run.created_at.isoformat(),
        "participants": [
            {
                "lane_id": l.id,
                "model": l.model,
                "provider_id": l.provider_id,
                "provider_name": providers[l.provider_id].name
                if l.provider_id in providers
                else "provider",
                "role": l.role,
            }
            for l in lanes
        ],
        "steps": [
            {
                "id": s.id,
                "lane_id": s.lane_id,
                "message_id": s.message_id,
                "round": s.round_index,
                "phase": s.phase,
                "label": s.label,
                "model": s.model,
                "verdict": s.verdict,
                "output": s.output_json or {},
                "degraded": s.degraded,
                "error": s.error,
                "latency_ms": s.latency_ms,
                "usage": s.usage_json,
            }
            for s in steps
        ],
    }


@router.get("")
def list_runs(
    limit: int = 25,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> list[dict]:
    rows = db.scalars(
        select(DeliberationRun)
        .where(DeliberationRun.user_id == user.id)
        .order_by(DeliberationRun.created_at.desc())
        .limit(max(1, min(limit, 200)))
    ).all()
    return [
        {
            "id": r.id,
            "session_id": r.session_id,
            "prompt": r.prompt[:200],
            "status": r.status,
            "converged": r.converged,
            "rounds_used": r.rounds_used,
            "total_calls": r.total_calls,
            "wall_ms": r.wall_ms,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.get("/leaderboard")
def leaderboard(user: User = Depends(current_user), db: DbSession = Depends(get_db)) -> dict:
    """Which models actually earn their seat, aggregated over every deliberation.

    ``influence`` is the share of peer-accepted claims that pointed at a model.
    ``capitulation`` is how often it changed position without naming what changed its
    mind — lower is better, and a high score is the signature of a model that simply
    agrees with whoever spoke last.
    """
    runs = db.scalars(
        select(DeliberationRun).where(
            DeliberationRun.user_id == user.id,
            DeliberationRun.status.in_(["converged", "no_consensus"]),
        )
    ).all()
    lane_models: dict[str, str] = {}
    influence: dict[str, list[float]] = {}
    capitulation: dict[str, list[float]] = {}
    for run in runs:
        session = db.get(ChatSession, run.session_id)
        if not session:
            continue
        for lane in session.lanes:
            lane_models[lane.id] = lane.model
        metrics = run.metrics_json or {}
        for lane_id, value in (metrics.get("influence") or {}).items():
            model = lane_models.get(lane_id)
            if model:
                influence.setdefault(model, []).append(float(value))
        for lane_id, value in (metrics.get("capitulation") or {}).items():
            model = lane_models.get(lane_id)
            if model:
                capitulation.setdefault(model, []).append(float(value))

    models = sorted(set(influence) | set(capitulation))
    return {
        "runs": len(runs),
        "models": [
            {
                "model": m,
                "influence": round(sum(influence[m]) / len(influence[m]), 3)
                if influence.get(m)
                else None,
                "capitulation": round(sum(capitulation[m]) / len(capitulation[m]), 3)
                if capitulation.get(m)
                else None,
                "appearances": len(influence.get(m) or capitulation.get(m) or []),
            }
            for m in models
        ],
    }


@router.post("/benchmark")
def benchmark(
    payload: BenchmarkRequest,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> StreamingResponse:
    """Run deliberation head-to-head against single / vote / synthesize on real prompts.

    This is the decision gate for the whole feature: if the council arm does not beat the
    cheap arms on the user's own questions, the extra calls are not buying anything and the
    honest answer is to use Quick mode.
    """
    prompts = [p.strip() for p in payload.prompts if p.strip()][:10]
    if not prompts:
        raise HTTPException(status_code=400, detail="Add at least one prompt")
    if len(payload.participants) < 2:
        raise HTTPException(status_code=400, detail="Pick at least two models for the panel")
    judge_ref = payload.judge or payload.participants[0]
    for member in payload.participants + [judge_ref]:
        provider = db.get(Provider, member.provider_id)
        if not provider or provider.user_id != user.id:
            raise HTTPException(status_code=400, detail="Unknown provider")

    panel = [
        {
            "key": f"p{i}",
            "provider_id": m.provider_id,
            "model": m.model,
        }
        for i, m in enumerate(payload.participants)
    ]
    judge = {"key": "judge", "provider_id": judge_ref.provider_id, "model": judge_ref.model}
    arms = [a for a in payload.arms if a in ARMS] or list(ARMS)
    rounds = max(1, min(payload.max_rounds, settings.DELIBERATION_MAX_ROUNDS))

    async def events():
        async for frame in run_benchmark(prompts, panel, judge, rounds, arms):
            yield frame

    return StreamingResponse(events(), media_type="text/event-stream")


@router.get("/by-session/{session_id}")
def get_by_session(
    session_id: str, user: User = Depends(current_user), db: DbSession = Depends(get_db)
) -> dict:
    """Resolve a deliberation from its session, so a session link can open the right page.

    A session can hold several runs once follow-ups are asked — open the latest.
    """
    run = db.scalars(
        select(DeliberationRun)
        .where(
            DeliberationRun.session_id == session_id, DeliberationRun.user_id == user.id
        )
        .order_by(DeliberationRun.created_at.desc())
    ).first()
    if not run:
        raise HTTPException(status_code=404, detail="Deliberation not found")
    return {"run_id": run.id}


@router.post("/classify")
async def classify(
    payload: ClassifyRequest,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> dict:
    """Cheap difficulty check so trivial questions don't get an expensive panel.

    Deliberation can make simple factual answers *worse* — models over-adapt to a
    confident-sounding peer instead of verifying. Steering those to a single model is a
    feature, not a limitation.
    """
    provider = pick_default_provider(db, user.id)
    text = (payload.prompt or "").strip()
    if not provider or not text:
        return {"complexity": "unknown", "recommend": "council", "reason": ""}
    model = provider.default_model or ((provider.models_json or [""])[0] if provider.models_json else "")
    if not model:
        return {"complexity": "unknown", "recommend": "council", "reason": ""}
    system = (
        "Classify how much a question benefits from a multi-model debate. Reply with ONE "
        'fenced json block: {"complexity":"trivial|simple|complex","reason":"<12 words"}.\n'
        "trivial = a lookup with one correct answer. simple = a short factual or "
        "definitional answer. complex = design, tradeoffs, strategy, ambiguity, or "
        "anything where reasonable experts could disagree."
    )
    try:
        llm = await build_provider(provider, db, model)
        raw = ""
        async for ev in llm.stream(
            [{"role": "system", "content": system}, {"role": "user", "content": text[:2000]}],
            None,
        ):
            if ev.type == "token" and ev.text:
                raw += ev.text
        data = extract_json(raw) or {}
    except Exception:  # noqa: BLE001 — classification is advisory only
        return {"complexity": "unknown", "recommend": "council", "reason": ""}
    complexity = str(data.get("complexity") or "unknown").lower()
    return {
        "complexity": complexity,
        "recommend": "single" if complexity in ("trivial", "simple") else "council",
        "reason": str(data.get("reason") or "")[:120],
    }


@router.post("")
def create_deliberation(
    payload: DeliberationCreate,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> dict:
    prompt = (payload.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="A question is required")
    members = payload.participants
    if len(members) < 2:
        raise HTTPException(status_code=400, detail="Pick at least two models for the panel")
    if len(members) > settings.DELIBERATION_MAX_PARTICIPANTS:
        raise HTTPException(
            status_code=400,
            detail=f"At most {settings.DELIBERATION_MAX_PARTICIPANTS} panelists",
        )
    for member in members + ([payload.judge] if payload.judge else []):
        provider = db.get(Provider, member.provider_id)
        if not provider or provider.user_id != user.id:
            raise HTTPException(status_code=400, detail="Unknown provider")

    title = (payload.title or "").strip()
    if not title:
        # The prompt doubles as the title; add an ellipsis so a long one doesn't look
        # like it was cut off by a bug.
        title = prompt if len(prompt) <= 70 else prompt[:69].rstrip() + "\u2026"
    session = ChatSession(user_id=user.id, title=title[:80], mode="deliberation")
    db.add(session)
    db.flush()

    for position, member in enumerate(members):
        db.add(
            Lane(
                session_id=session.id,
                provider_id=member.provider_id,
                model=member.model,
                position=position,
                role="responder",
            )
        )
    if payload.judge:
        db.add(
            Lane(
                session_id=session.id,
                provider_id=payload.judge.provider_id,
                model=payload.judge.model,
                position=len(members),
                role="judge",
            )
        )

    turn = Turn(session_id=session.id, order_index=0, content=prompt)
    db.add(turn)
    db.flush()

    # Bind any uploaded images to the question turn, so every panelist sees them.
    for att_id in payload.attachment_ids:
        att = db.get(Attachment, att_id)
        if att and att.user_id == user.id:
            att.turn_id = turn.id
            db.add(att)

    run = DeliberationRun(
        user_id=user.id,
        session_id=session.id,
        turn_id=turn.id,
        prompt=prompt,
        status="pending",
        config_json={
            "max_rounds": max(
                1, min(payload.max_rounds, settings.DELIBERATION_MAX_ROUNDS)
            ),
            "synthesis": payload.synthesis,
            "minority_report": payload.minority_report,
            "critique_synthesis": payload.critique_synthesis,
            "mode": "quick" if payload.mode == "quick" else "council",
            "evidence": payload.evidence,
        },
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return {"run_id": run.id, "session_id": session.id, "turn_id": turn.id}


@router.get("/{run_id}")
def get_run(
    run_id: str, user: User = Depends(current_user), db: DbSession = Depends(get_db)
) -> dict:
    return _serialize(db, _get_run(db, user, run_id))


@router.post("/{run_id}/stream")
def stream(
    run_id: str, user: User = Depends(current_user), db: DbSession = Depends(get_db)
) -> StreamingResponse:
    """Start the run (or rejoin one already in flight) and stream its SSE frames.

    The run is driven by a background task rather than by this request, so navigating away
    does not cancel it — reconnecting replays everything that happened meanwhile.
    """
    run = _get_run(db, user, run_id)
    run_id = run.id

    async def events():
        async for frame in stream_run(run_id):
            yield frame

    return StreamingResponse(events(), media_type="text/event-stream")


@router.post("/{run_id}/stop")
def stop(
    run_id: str, user: User = Depends(current_user), db: DbSession = Depends(get_db)
) -> dict:
    run = _get_run(db, user, run_id)
    return {"stopping": request_stop(run.id)}


@router.post("/{run_id}/continue")
def continue_in_chat(
    run_id: str,
    payload: ContinueRequest = ContinueRequest(),
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> dict:
    """Fork an answer into a normal chat so the user can keep talking.

    Defaults to the synthesis. Pass ``step_id`` to carry on from one panelist's answer
    instead — the wording the panel agreed on is not always the one worth pursuing, and
    the dissenting draft is often the interesting one.
    """
    run = _get_run(db, user, run_id)
    step = None
    if payload.step_id:
        step = db.get(DeliberationStep, payload.step_id)
        if step is None or step.run_id != run.id:
            raise HTTPException(status_code=404, detail="Step not found")
        output = step.output_json or {}
        answer = str(output.get("revised_answer") or output.get("answer") or "").strip()
        if not answer:
            raise HTTPException(status_code=400, detail="That step has no answer to continue")
    else:
        answer = (run.synthesis or "").strip()
        if not answer:
            raise HTTPException(status_code=400, detail="This deliberation has no synthesis yet")

    source = db.get(ChatSession, run.session_id)
    lanes = sorted(source.lanes, key=lambda l: l.position) if source else []
    panel = [l for l in lanes if l.role == "responder"]
    if not panel:
        raise HTTPException(status_code=400, detail="No panelists to continue with")
    # Continue with whichever model wrote the answer — including an off-panel judge, which
    # is exactly who writes the synthesis.
    start = next((l for l in lanes if step is not None and l.id == step.lane_id), panel[0])

    chat = ChatSession(
        user_id=user.id,
        title=f"{source.title}"[:80] if source else "Deliberation follow-up",
        mode="compare",
    )
    db.add(chat)
    db.flush()
    lane = Lane(
        session_id=chat.id,
        provider_id=start.provider_id,
        model=start.model,
        position=0,
        role="responder",
    )
    db.add(lane)
    turn = Turn(session_id=chat.id, order_index=0, content=run.prompt)
    db.add(turn)
    db.flush()
    from ..models import LaneMessage

    db.add(
        LaneMessage(
            lane_id=lane.id,
            turn_id=turn.id,
            role="assistant",
            content=answer,
            order_index=0,
        )
    )
    db.commit()
    return {"session_id": chat.id}


@router.post("/{run_id}/export")
def export_run(
    run_id: str,
    fmt: str = "pdf",
    payload: MessageExportRequest | None = None,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> dict:
    """Export the whole deliberation — rounds, objections, synthesis, dissent.

    ``fmt=json`` is the audit trail: every step's exact input, output and verdict.
    """
    import os

    from ..export import export_deliberation
    from ..models import GeneratedFile
    from .sessions import _decode_diagrams

    run = _get_run(db, user, run_id)
    fmt = (fmt or "pdf").lower()
    if fmt not in ("pdf", "md", "docx", "json"):
        raise HTTPException(status_code=400, detail=f"Unsupported export format: {fmt}")
    try:
        # Mermaid needs a browser, so the panel view hands over the diagrams it rendered.
        stored_name, download_name, mime = export_deliberation(
            db, run, fmt, _decode_diagrams(payload)
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail=f"Export failed: {log_and_describe(exc, f'deliberation {run.id} export failed')}",
        )
    path = os.path.join(settings.UPLOAD_DIR, "generated", stored_name)
    db.add(
        GeneratedFile(
            user_id=user.id,
            session_id=run.session_id,
            stored_name=stored_name,
            download_name=download_name,
            mime_type=mime,
            size_bytes=os.path.getsize(path) if os.path.exists(path) else 0,
            kind=fmt,
        )
    )
    db.commit()
    return {"url": f"/api/files/{stored_name}?name={download_name}", "download_name": download_name}


@router.post("/{run_id}/followup")
def followup(
    run_id: str,
    payload: FollowupRequest,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> dict:
    """Ask the same panel a follow-up, keeping the session, the panel and the context.

    A deliberation answers one question, so a follow-up is a *new run* rather than another
    turn inside the old one — the protocol (blind drafts, barrier, verdict gate) has to
    start over for the convergence numbers to mean anything. What carries over is the
    panel, the settings, and the answer they already agreed on.
    """
    prompt = (payload.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="A question is required")
    parent = _get_run(db, user, run_id)
    if is_running(parent.id):
        raise HTTPException(status_code=400, detail="This deliberation is still running")
    session = db.get(ChatSession, parent.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Deliberation session is gone")
    panel = [l for l in session.lanes if l.role == "responder"]
    if len(panel) < 2:
        raise HTTPException(status_code=400, detail="No panel to ask")

    order = max((t.order_index for t in session.turns), default=-1) + 1
    turn = Turn(session_id=session.id, order_index=order, content=prompt)
    db.add(turn)
    db.flush()
    for att_id in payload.attachment_ids:
        att = db.get(Attachment, att_id)
        if att and att.user_id == user.id:
            att.turn_id = turn.id
            db.add(att)

    config = dict(parent.config_json or {})
    config["parent_run_id"] = parent.id
    # What the panel already settled, given to every panelist as background. Trimmed:
    # the point is continuity, not re-litigating the previous answer.
    previous = (parent.synthesis or "").strip()
    if previous:
        config["context"] = (
            "EARLIER IN THIS DELIBERATION\n\n"
            f"Question: {parent.prompt}\n\n"
            f"The panel's agreed answer:\n{previous[:6000]}"
        )
    run = DeliberationRun(
        user_id=user.id,
        session_id=session.id,
        turn_id=turn.id,
        prompt=prompt,
        status="pending",
        config_json=config,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return {"run_id": run.id, "session_id": session.id, "turn_id": turn.id}


@router.post("/{run_id}/rerun")
def rerun(
    run_id: str, user: User = Depends(current_user), db: DbSession = Depends(get_db)
) -> dict:
    """Run the same question past the same panel again, in a fresh session.

    Mostly for the case where a panelist failed outright (an expired provider token, a
    rate limit): a failed panelist leaves the approval denominator, so the run that
    survived it is weaker evidence than it looks. Re-running is honest; patching a single
    step back into a finished round would not be.
    """
    source = _get_run(db, user, run_id)
    old_session = db.get(ChatSession, source.session_id)
    lanes = sorted(old_session.lanes, key=lambda l: l.position) if old_session else []
    panel = [l for l in lanes if l.role == "responder"]
    if len(panel) < 2:
        raise HTTPException(status_code=400, detail="No panel to re-run")

    session = ChatSession(
        user_id=user.id,
        title=(old_session.title if old_session else source.prompt)[:80],
        mode="deliberation",
    )
    db.add(session)
    db.flush()
    for position, lane in enumerate(lanes):
        db.add(
            Lane(
                session_id=session.id,
                provider_id=lane.provider_id,
                model=lane.model,
                position=position,
                role=lane.role,
            )
        )
    turn = Turn(session_id=session.id, order_index=0, content=source.prompt)
    db.add(turn)
    db.flush()
    old_turn = db.get(Turn, source.turn_id)
    for att in old_turn.attachments if old_turn else []:
        # Attachments are shared rows; copy the reference by re-uploading is overkill, so
        # the re-run simply carries the same files by pointing a copy at the new turn.
        db.add(
            Attachment(
                user_id=att.user_id,
                turn_id=turn.id,
                filename=att.filename,
                mime_type=att.mime_type,
                size_bytes=att.size_bytes,
                storage_path=att.storage_path,
                kind=att.kind,
                extracted_text=att.extracted_text,
            )
        )
    config = dict(source.config_json or {})
    config.pop("parent_run_id", None)
    run = DeliberationRun(
        user_id=user.id,
        session_id=session.id,
        turn_id=turn.id,
        prompt=source.prompt,
        status="pending",
        config_json=config,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return {"run_id": run.id, "session_id": session.id, "turn_id": turn.id}


@router.delete("/{run_id}", status_code=204)
def delete_run(
    run_id: str, user: User = Depends(current_user), db: DbSession = Depends(get_db)
) -> None:
    run = _get_run(db, user, run_id)
    request_stop(run.id)
    session = db.get(ChatSession, run.session_id)
    siblings = db.scalars(
        select(DeliberationRun).where(
            DeliberationRun.session_id == run.session_id, DeliberationRun.id != run.id
        )
    ).all()
    db.delete(run)
    # Follow-ups share one session; only the last run standing takes it down with it.
    if session and not siblings:
        db.delete(session)
    db.commit()
