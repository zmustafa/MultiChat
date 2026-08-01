"""HTTP surface for Model Deliberation.

A deliberation is an ordinary ``Session`` with ``mode="deliberation"``: panelists are
``Lane`` rows and every round's answer is a ``LaneMessage``. That means transcript export,
search, artifacts and the per-message PDF all work on it without special-casing. The
``DeliberationRun`` row holds only what is unique to the protocol.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..config import settings
from ..db import get_db
from ..benchmark import ARMS, run_benchmark
from ..deliberation import is_running, request_stop, stream_run
from ..models import (
    Attachment,
    DeliberationRun,
    DeliberationStep,
    Lane,
    Provider,
    Session as ChatSession,
    Turn,
    User,
)
from ..providers.registry import build_provider, pick_default_provider
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
    return {
        "id": run.id,
        "session_id": run.session_id,
        "turn_id": run.turn_id,
        "title": session.title if session else "",
        "status": run.status,
        "running": is_running(run.id),
        "prompt": run.prompt,
        "images": [
            {"id": a.id, "filename": a.filename, "url": f"/api/uploads/{a.id}"}
            for a in (turn.attachments if turn else [])
            if a.kind == "image"
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
    """Resolve a deliberation from its session, so a session link can open the right page."""
    run = db.scalars(
        select(DeliberationRun).where(
            DeliberationRun.session_id == session_id, DeliberationRun.user_id == user.id
        )
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
    run_id: str, user: User = Depends(current_user), db: DbSession = Depends(get_db)
) -> dict:
    """Fork the synthesized answer into a normal chat so the user can keep talking.

    A deliberation answers exactly one question; follow-ups belong in a regular session.
    """
    run = _get_run(db, user, run_id)
    if not run.synthesis:
        raise HTTPException(status_code=400, detail="This deliberation has no synthesis yet")
    source = db.get(ChatSession, run.session_id)
    lanes = sorted(source.lanes, key=lambda l: l.position) if source else []
    panel = [l for l in lanes if l.role == "responder"]
    if not panel:
        raise HTTPException(status_code=400, detail="No panelists to continue with")

    chat = ChatSession(
        user_id=user.id,
        title=f"{source.title}"[:80] if source else "Deliberation follow-up",
        mode="compare",
    )
    db.add(chat)
    db.flush()
    lane = Lane(
        session_id=chat.id,
        provider_id=panel[0].provider_id,
        model=panel[0].model,
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
            content=run.synthesis,
            order_index=0,
        )
    )
    db.commit()
    return {"session_id": chat.id}


@router.post("/{run_id}/export")
def export_run(
    run_id: str, user: User = Depends(current_user), db: DbSession = Depends(get_db)
) -> dict:
    """Export the whole deliberation — rounds, objections, synthesis, dissent — as a PDF."""
    import os

    from ..export import export_deliberation_pdf
    from ..models import GeneratedFile

    run = _get_run(db, user, run_id)
    try:
        stored_name, download_name, mime = export_deliberation_pdf(db, run)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Export failed: {exc}")
    path = os.path.join(settings.UPLOAD_DIR, "generated", stored_name)
    db.add(
        GeneratedFile(
            user_id=user.id,
            session_id=run.session_id,
            stored_name=stored_name,
            download_name=download_name,
            mime_type=mime,
            size_bytes=os.path.getsize(path) if os.path.exists(path) else 0,
            kind="pdf",
        )
    )
    db.commit()
    return {"url": f"/api/files/{stored_name}?name={download_name}", "download_name": download_name}


@router.delete("/{run_id}", status_code=204)
def delete_run(
    run_id: str, user: User = Depends(current_user), db: DbSession = Depends(get_db)
) -> None:
    run = _get_run(db, user, run_id)
    request_stop(run.id)
    session = db.get(ChatSession, run.session_id)
    db.delete(run)
    if session:
        db.delete(session)
    db.commit()
