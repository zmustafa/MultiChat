"""Model Deliberation: a panel of models answers, reviews each other, and settles.

The orchestrator owns the entire graph; models are stateless functions of
``(question, anonymized peer output, round instructions)``. No model keeps state between
calls, which is what makes the protocol auditable — every verdict can be traced to the
exact input that produced it.

Protocol
--------
1. **Draft** — every panelist answers blind, in parallel. Nobody sees a peer.
2. **Barrier** — all drafts are persisted before *any* of them is released into round 1.
3. **Critique** — each panelist reviews all peers (anonymized, order shuffled per viewer)
   and revises its own answer. Repeated up to ``max_rounds``.
4. **Synthesis** — a model that did *not* win the round merges what survived, and reports
   what did not, as a minority report.
5. **Synthesis critique** — a different model checks the synthesis for papered-over
   disagreement; the synthesizer revises once.

Why the guards exist
--------------------
Left alone, LLM panels agree with each other far too readily: the measured correlation
between sycophancy and *wrong* answers is high, and it gets worse from round three. So
convergence is judged on explicit verdicts rather than on how similar the prose looks,
peers are anonymous, peer confidence is hidden, a model must name what changed its mind,
and no round can be carried by a single approval.
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import select

from .broadcast import sse
from .config import settings
from .convergence import (
    APPROVE,
    REQUEST_CHANGES,
    VERDICTS,
    borda_count,
    panel_metrics,
    score_round,
    should_continue,
)
from .db import SessionLocal
from .models import (
    DeliberationRun,
    DeliberationStep,
    Lane,
    LaneMessage,
    Provider,
    Session as ChatSession,
)
from .structured import call_structured

# --------------------------------------------------------------------------------------
# Prompts — the actual product. Every clause here counters a documented failure mode.
# --------------------------------------------------------------------------------------

_CLAIMS_NOTE = (
    "Break your answer into CLAIMS: the individual assertions a reviewer could accept or "
    "reject on their own. A claim is one checkable statement, not a paragraph. Give each a "
    "short id (c1, c2, ...)."
)

_EVIDENCE_NOTE = (
    "\n\nEVIDENCE: every claim of kind 'fact' must carry a `support` field naming what it "
    "rests on — a specification, vendor documentation, a measurement, or published data. If "
    "a claim is really your judgement rather than a fact, mark its kind honestly instead of "
    "inventing a source. Never cite a document you are not sure exists."
)

VOTE_SYSTEM = (
    "You are judging answers to the question below.\n\n"
    "The candidate answers are ANONYMIZED and shuffled. One of them may be your own — you "
    "are not told which, and you must not try to work it out. Judge only the content.\n\n"
    "Rank them best-first on: correctness, completeness, and whether the recommendation is "
    "concrete enough to act on. Verbosity is not quality; a longer answer that hedges is "
    "worse than a shorter one that commits."
)

VOTE_SCHEMA = """{
  "ranking": ["Answer A", "Answer C", "Answer B"],
  "reason": "one sentence on why the top answer wins"
}"""

DRAFT_SYSTEM = (
    "You are an expert analyst answering a question independently.\n\n"
    "Several other models are answering the same question right now. You cannot see them "
    "and they cannot see you. Answer as well as you can on your own merits.\n\n"
    f"{_CLAIMS_NOTE}\n\n"
    "Write the answer itself in Markdown. Use fenced code blocks for code and ```mermaid "
    "fences for diagrams where they genuinely help."
)

DRAFT_SCHEMA = """{
  "answer": "your full answer, in Markdown",
  "reasoning_summary": "2-3 sentences on how you reached it",
  "assumptions": ["anything you had to assume"],
  "claims": [{"id": "c1", "text": "one checkable assertion", "kind": "fact|recommendation|estimate", "support": "what this rests on, or null"}],
  "confidence": 0.0
}"""

CRITIQUE_SYSTEM = (
    "You are reviewing a panel.\n\n"
    "Several peers answered the same question independently. Their answers are below, "
    "ANONYMIZED — you do not know which model wrote which, and you must not guess or "
    "speculate about it. Judge the content, never the source.\n\n"
    "Your job, in order:\n"
    "1. Review every peer claim. ACCEPT it or REJECT it. Every rejection MUST carry a "
    "specific, checkable reason. 'I disagree' or 'this is unclear' is not a reason.\n"
    "2. Vague agreement is worthless. Look hard before you approve. If you genuinely find "
    "nothing material to object to, return verdict APPROVE and say so plainly.\n"
    "3. Do NOT change your position unless a peer gave you NEW EVIDENCE or exposed a "
    "concrete error in your reasoning. If you do change, name the exact peer claim id that "
    "caused it in change_trigger. Changing your answer to be agreeable is a failure, and "
    "it will be measured.\n"
    "4. Produce your revised answer. If nothing changed, restate your answer in full "
    "anyway — the revised answer must always stand alone.\n\n"
    "VERDICTS:\n"
    "  APPROVE          - the panel's work is sound; you have no material objection left.\n"
    "  REQUEST_CHANGES  - you have specific objections that must be resolved.\n"
    "  REJECT           - a peer answer is fundamentally wrong, unsafe, or misleading.\n\n"
    f"{_CLAIMS_NOTE}"
)

CRITIQUE_SCHEMA = """{
  "verdict": "APPROVE|REQUEST_CHANGES|REJECT",
  "accepted_claims": [{"peer": "Peer A", "claim_id": "A/c1", "note": "why it holds"}],
  "rejected_claims": [{"peer": "Peer A", "claim_id": "A/c2", "reason": "the specific, checkable problem"}],
  "no_material_disagreement": false,
  "position_changed": false,
  "change_trigger": null,
  "revised_answer": "your full standalone answer, in Markdown",
  "claims": [{"id": "c1", "text": "one checkable assertion", "kind": "fact|recommendation|estimate", "support": "what this rests on, or null"}],
  "confidence": 0.0
}"""

SYNTHESIS_SYSTEM = (
    "You are the editor. A panel of experts has just finished deliberating on the question "
    "below. You did not take part.\n\n"
    "Produce ONE authoritative answer built from what survived review. Then, separately and "
    "without burying it, report what the panel did NOT settle.\n\n"
    "Rules:\n"
    "- Never mention models, panels, peers or that several answers existed. Write it as one "
    "expert's answer.\n"
    "- Keep the code, tables and ```mermaid diagrams that earned their place.\n"
    "- Where peers disagreed and never resolved it, put the competing positions in "
    "minority_report, concretely. Do not manufacture agreement that isn't there.\n"
    "- do_now / consider_later / skip are short, imperative, and directly actionable."
)

SYNTHESIS_SCHEMA = """{
  "answer": "the merged answer, in Markdown",
  "minority_report": "unresolved disagreements and the competing positions, or null",
  "do_now": ["short imperative action"],
  "consider_later": ["short imperative action"],
  "skip": ["what to explicitly not do"]
}"""

SYNTHESIS_CRITIQUE_SYSTEM = (
    "You are auditing an editor's synthesis of an expert panel.\n\n"
    "You are looking for exactly three failures:\n"
    "1. The synthesis states as settled something the panel actually disputed.\n"
    "2. The synthesis contains a claim no panelist made.\n"
    "3. The synthesis drops a substantive point that survived review.\n\n"
    "Be specific and be brief. If the synthesis is faithful, say so."
)

SYNTHESIS_CRITIQUE_SCHEMA = """{
  "faithful": true,
  "issues": [{"severity": "high|medium|low", "text": "what is wrong and where"}]
}"""

SYNTHESIS_REVISE_SYSTEM = (
    "You wrote the synthesis below. An auditor raised the issues listed. Fix them and "
    "return the corrected synthesis in the same shape. Do not fix what was not raised."
)


# --------------------------------------------------------------------------------------
# Run registry: lets a deliberation outlive the HTTP request that started it
# --------------------------------------------------------------------------------------


class _Hub:
    """Buffers a run's SSE frames so a client that navigates away can rejoin live."""

    def __init__(self) -> None:
        self.frames: list[str] = []
        self.subscribers: set[asyncio.Queue] = set()
        self.done = False

    def publish(self, frame: str) -> None:
        self.frames.append(frame)
        for queue in list(self.subscribers):
            queue.put_nowait(frame)

    def finish(self) -> None:
        self.done = True
        for queue in list(self.subscribers):
            queue.put_nowait(None)

    async def subscribe(self) -> AsyncIterator[str]:
        queue: asyncio.Queue = asyncio.Queue()
        # Replay everything that already happened, then tail.
        for frame in list(self.frames):
            yield frame
        if self.done:
            return
        self.subscribers.add(queue)
        try:
            while True:
                frame = await queue.get()
                if frame is None:
                    return
                yield frame
        finally:
            self.subscribers.discard(queue)


_hubs: dict[str, _Hub] = {}
_tasks: dict[str, asyncio.Task] = {}
_cancels: dict[str, asyncio.Event] = {}
HUB_TTL_SECONDS = 120


def is_running(run_id: str) -> bool:
    task = _tasks.get(run_id)
    return bool(task and not task.done())


def request_stop(run_id: str) -> bool:
    """Ask a running deliberation to wind up at the next safe point."""
    event = _cancels.get(run_id)
    if event:
        event.set()
        return True
    return False


def start_run(run_id: str) -> _Hub:
    """Start (or rejoin) a deliberation. Safe to call repeatedly."""
    hub = _hubs.get(run_id)
    if hub and is_running(run_id):
        return hub
    if hub and hub.done:
        return hub
    hub = _Hub()
    _hubs[run_id] = hub
    _cancels[run_id] = asyncio.Event()
    _tasks[run_id] = asyncio.create_task(_drive(run_id, hub))
    return hub


async def stream_run(run_id: str) -> AsyncIterator[str]:
    """SSE frames for a run, starting it if it isn't already going."""
    hub = start_run(run_id)
    async for frame in hub.subscribe():
        yield frame


def _schedule_cleanup(run_id: str) -> None:
    async def later() -> None:
        await asyncio.sleep(HUB_TTL_SECONDS)
        _hubs.pop(run_id, None)
        _tasks.pop(run_id, None)
        _cancels.pop(run_id, None)

    asyncio.create_task(later())


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------

_LABELS = ["Peer A", "Peer B", "Peer C", "Peer D", "Peer E", "Peer F"]


class _Finished(Exception):
    """Internal signal: this run is complete and should skip straight to teardown."""


def _truncate(text: str, limit: int | None = None) -> str:
    limit = limit or settings.DELIBERATION_PEER_CHARS
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "\n…(truncated)"


def _validate_critique(data: dict) -> str | None:
    """Enforce the protocol rules the prompt asks for — a reviewer cannot dismiss silently."""
    verdict = str(data.get("verdict") or "").strip().upper()
    if verdict not in VERDICTS:
        return f"verdict must be one of {', '.join(VERDICTS)}"
    rejected = data.get("rejected_claims") or []
    if not isinstance(rejected, list):
        return "rejected_claims must be a list"
    for item in rejected:
        if not isinstance(item, dict) or not str(item.get("reason") or "").strip():
            return "every entry in rejected_claims needs a specific 'reason'"
    if verdict != APPROVE and not rejected:
        return f"verdict {verdict} requires at least one entry in rejected_claims"
    if data.get("position_changed") and not str(data.get("change_trigger") or "").strip():
        return "position_changed requires change_trigger naming the peer claim that caused it"
    if not str(data.get("revised_answer") or "").strip():
        return "revised_answer must not be empty"
    return None


def unsupported_facts(output: dict) -> list[str]:
    """Factual claims asserted with no stated basis."""
    missing = []
    for claim in output.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        if str(claim.get("kind") or "").lower() != "fact":
            continue
        if not str(claim.get("support") or "").strip():
            missing.append(str(claim.get("id") or "?"))
    return missing


def _require_evidence(data: dict) -> str | None:
    missing = unsupported_facts(data)
    if missing:
        return (
            "these claims are marked kind='fact' but have no 'support': "
            f"{', '.join(missing)} — either state what each rests on or change its kind"
        )
    return None


def _validate_vote(candidates: list[str]):
    def check(data: dict) -> str | None:
        ranking = data.get("ranking")
        if not isinstance(ranking, list) or not ranking:
            return "ranking must be a non-empty list of candidate labels"
        unknown = [r for r in ranking if r not in candidates]
        if unknown:
            return f"ranking contains unknown labels: {', '.join(map(str, unknown[:3]))}"
        return None

    return check


def _answer_of(output: dict) -> str:
    return str(output.get("revised_answer") or output.get("answer") or "").strip()


def _claim_lines(output: dict, prefix: str) -> str:
    lines = []
    for claim in output.get("claims") or []:
        if isinstance(claim, dict) and claim.get("text"):
            lines.append(f"  [{prefix}/{claim.get('id', '?')}] {claim['text']}")
        elif isinstance(claim, str):
            lines.append(f"  [{prefix}/?] {claim}")
    return "\n".join(lines) or "  (no discrete claims provided)"


def _peer_block(label: str, output: dict, letter: str) -> str:
    """One peer's contribution as the reviewer sees it.

    Deliberately omits the peer's confidence: a stated confidence anchors reviewers hard,
    and it is not well calibrated enough to be worth that cost.
    """
    return (
        f"--- {label} ---\n"
        f"{_truncate(_answer_of(output))}\n\n"
        f"{label} CLAIMS:\n{_claim_lines(output, letter)}\n"
    )


def _sse_step(event: str, payload: dict) -> str:
    return sse(event, payload)


# --------------------------------------------------------------------------------------
# One model call inside the run
# --------------------------------------------------------------------------------------


async def _run_step(
    *,
    run_id: str,
    lane_id: str | None,
    model: str,
    provider_id: str,
    round_index: int,
    phase: str,
    label: str | None,
    system: str,
    user: str,
    schema: str,
    required: tuple[str, ...],
    validate: Any,
    hub: _Hub,
    step_id: str,
) -> dict:
    """Execute one structured model call, publish progress, and persist the step."""
    db = SessionLocal()
    chars = {"n": 0, "sent": 0}

    def on_token(delta: str) -> None:
        chars["n"] += len(delta)
        # The stream is raw JSON, which is useless to show. Publish a throttled progress
        # tick instead so the UI can prove the model is alive.
        if chars["n"] - chars["sent"] >= 160:
            chars["sent"] = chars["n"]
            hub.publish(_sse_step("step_progress", {"step_id": step_id, "chars": chars["n"]}))

    result = None
    error: str | None = None
    try:
        provider = db.get(Provider, provider_id)
        if provider is None:
            raise RuntimeError("provider not found")
        result = await call_structured(
            provider,
            db,
            model,
            system=system,
            user=user,
            schema=schema,
            required=required,
            validate=validate,
            on_token=on_token,
            repair_attempts=settings.DELIBERATION_REPAIR_ATTEMPTS,
            fallback_field="revised_answer" if phase == "critique" else "answer",
        )
    except Exception as exc:  # noqa: BLE001 — a failed panelist must not fail the run
        error = str(exc)

    payload = {
        "step_id": step_id,
        "round": round_index,
        "phase": phase,
        "lane_id": lane_id,
        "label": label,
        "model": model,
    }
    try:
        step = DeliberationStep(
            id=step_id,
            run_id=run_id,
            lane_id=lane_id,
            round_index=round_index,
            phase=phase,
            label=label,
            model=model,
            input_json={"system": system, "user": user},
            output_json=(result.data if result else {}),
            raw_text=(result.raw[:20000] if result else None),
            degraded=bool(result.degraded) if result else False,
            verdict=(str(result.data.get("verdict")).upper() if result and result.data.get("verdict") else None),
            error=error,
            latency_ms=(result.latency_ms if result else None),
            usage_json=(result.usage if result else None),
        )
        db.add(step)
        # Mirror the answer into the normal transcript so export, search and the
        # per-message PDF all work on a deliberation with no special-casing.
        run = db.get(DeliberationRun, run_id)
        if run and lane_id and result and not error:
            body = _answer_of(result.data)
            if body:
                db.add(
                    LaneMessage(
                        lane_id=lane_id,
                        turn_id=run.turn_id,
                        role="assistant",
                        content=body,
                        order_index=round_index,
                        usage_json=result.usage,
                        latency_ms=result.latency_ms,
                        ttft_ms=result.ttft_ms,
                    )
                )
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        error = error or str(exc)
    finally:
        db.close()

    if error:
        hub.publish(_sse_step("step_error", {**payload, "detail": error}))
        return {"ok": False, "error": error, "output": {}, "verdict": None}

    assert result is not None
    hub.publish(
        _sse_step(
            "step_done",
            {
                **payload,
                "verdict": str(result.data.get("verdict") or "").upper() or None,
                "degraded": result.degraded,
                "latency_ms": result.latency_ms,
                "usage": result.usage,
                "output": result.data,
            },
        )
    )
    return {
        "ok": True,
        "output": result.data,
        "degraded": result.degraded,
        "verdict": str(result.data.get("verdict") or "").upper() or None,
        "latency_ms": result.latency_ms,
    }


async def _fan_out(jobs: list[Any], concurrency: int) -> list[dict]:
    """Run one round's calls in parallel, bounded, never letting one failure kill the rest."""
    semaphore = asyncio.Semaphore(concurrency)
    results: list[dict] = [{} for _ in jobs]

    async def guarded(index: int, job: Any) -> None:
        async with semaphore:
            try:
                results[index] = await job()
            except Exception as exc:  # noqa: BLE001
                results[index] = {"ok": False, "error": str(exc), "output": {}, "verdict": None}

    await asyncio.gather(*(guarded(i, j) for i, j in enumerate(jobs)))
    return results


# --------------------------------------------------------------------------------------
# The state machine
# --------------------------------------------------------------------------------------


def _load_context(run_id: str) -> dict | None:
    """Snapshot everything the run needs, so the driver holds no DB session while calling."""
    db = SessionLocal()
    try:
        run = db.get(DeliberationRun, run_id)
        if run is None:
            return None
        session = db.get(ChatSession, run.session_id)
        if session is None:
            return None
        lanes = sorted(session.lanes, key=lambda l: l.position)
        providers = {p.id: p for p in db.scalars(select(Provider)).all()}
        panel = [
            {
                "lane_id": l.id,
                "provider_id": l.provider_id,
                "model": l.model,
                "provider_name": providers[l.provider_id].name if l.provider_id in providers else "provider",
            }
            for l in lanes
            if l.role == "responder"
        ]
        judge_lane = next((l for l in lanes if l.role == "judge"), None)
        judge = (
            {
                "lane_id": judge_lane.id,
                "provider_id": judge_lane.provider_id,
                "model": judge_lane.model,
                "provider_name": providers[judge_lane.provider_id].name
                if judge_lane.provider_id in providers
                else "provider",
            }
            if judge_lane
            else None
        )
        return {
            "prompt": run.prompt,
            "config": dict(run.config_json or {}),
            "panel": panel,
            "judge": judge,
            "session_id": run.session_id,
            "turn_id": run.turn_id,
        }
    finally:
        db.close()


def _save(run_id: str, **fields: Any) -> None:
    db = SessionLocal()
    try:
        run = db.get(DeliberationRun, run_id)
        if run is None:
            return
        for key, value in fields.items():
            setattr(run, key, value)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
    finally:
        db.close()


def _set_lane_states(lane_ids: list[str], state: str) -> None:
    db = SessionLocal()
    try:
        for lane_id in lane_ids:
            lane = db.get(Lane, lane_id)
            if lane:
                lane.state = state
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
    finally:
        db.close()


def _draft_job(hub: _Hub, run_id: str, participant: dict, prompt: str, step_id: str, evidence: bool = False) -> Any:
    async def job() -> dict:
        hub.publish(
            _sse_step(
                "step_start",
                {
                    "step_id": step_id,
                    "round": 0,
                    "phase": "draft",
                    "lane_id": participant["lane_id"],
                    "model": participant["model"],
                },
            )
        )
        return await _run_step(
            run_id=run_id,
            lane_id=participant["lane_id"],
            model=participant["model"],
            provider_id=participant["provider_id"],
            round_index=0,
            phase="draft",
            label=None,
            system=DRAFT_SYSTEM + (_EVIDENCE_NOTE if evidence else ""),
            user=f"QUESTION:\n{prompt}",
            schema=DRAFT_SCHEMA,
            required=("answer",),
            validate=_require_evidence if evidence else None,
            hub=hub,
            step_id=step_id,
        )

    return job


def _critique_job(
    hub: _Hub,
    run_id: str,
    participant: dict,
    prompt: str,
    own: dict,
    peers: list[tuple[str, str, dict]],
    round_index: int,
    step_id: str,
) -> Any:
    """``peers`` is [(label, letter, output)] already shuffled for this viewer."""
    blocks = "\n".join(_peer_block(label, output, letter) for label, letter, output in peers)
    user = (
        f"QUESTION:\n{prompt}\n\n"
        f"YOUR PREVIOUS ANSWER:\n{_truncate(_answer_of(own))}\n\n"
        f"YOUR CLAIMS:\n{_claim_lines(own, 'self')}\n\n"
        f"PEER ANSWERS (anonymized — {len(peers)} peer(s)):\n{blocks}"
    )

    async def job() -> dict:
        hub.publish(
            _sse_step(
                "step_start",
                {
                    "step_id": step_id,
                    "round": round_index,
                    "phase": "critique",
                    "lane_id": participant["lane_id"],
                    "model": participant["model"],
                },
            )
        )
        return await _run_step(
            run_id=run_id,
            lane_id=participant["lane_id"],
            model=participant["model"],
            provider_id=participant["provider_id"],
            round_index=round_index,
            phase="critique",
            label=None,
            system=CRITIQUE_SYSTEM,
            user=user,
            schema=CRITIQUE_SCHEMA,
            required=("verdict", "revised_answer"),
            validate=_validate_critique,
            hub=hub,
            step_id=step_id,
        )

    return job


def _assign_peers(
    viewer: dict, participants: list[dict], outputs: dict[str, dict]
) -> tuple[list[tuple[str, str, dict]], dict[str, str]]:
    """Label a viewer's peers anonymously, in a random order.

    Labels are per-viewer and reshuffled every round so that neither identity nor position
    can be used as a proxy for authority.
    """
    others = [p for p in participants if p["lane_id"] != viewer["lane_id"] and outputs.get(p["lane_id"])]
    random.shuffle(others)
    peers: list[tuple[str, str, dict]] = []
    label_to_lane: dict[str, str] = {}
    for index, peer in enumerate(others):
        label = _LABELS[index % len(_LABELS)]
        letter = label.split()[-1]
        peers.append((label, letter, outputs[peer["lane_id"]]))
        label_to_lane[label] = peer["lane_id"]
    return peers, label_to_lane


def _translate_peers(output: dict, label_to_lane: dict[str, str]) -> dict:
    """Rewrite anonymous peer labels back to lane ids so metrics survive reshuffling."""
    translated = dict(output)
    for key in ("accepted_claims", "rejected_claims"):
        items = output.get(key)
        if not isinstance(items, list):
            continue
        rewritten = []
        for item in items:
            if isinstance(item, dict):
                item = dict(item)
                lane = label_to_lane.get(str(item.get("peer") or ""))
                if lane:
                    item["peer"] = lane
            rewritten.append(item)
        translated[key] = rewritten
    return translated


def _vote_job(
    hub: _Hub,
    run_id: str,
    voter: dict,
    prompt: str,
    slate: list[tuple[str, dict]],
    step_id: str,
) -> Any:
    """Ask one panelist to rank the whole anonymized slate, its own answer included.

    Every voter sees the same labels in the same order — a shared slate is what makes the
    ballots comparable — but nobody is told which entry is theirs.
    """
    blocks = "\n".join(
        f"--- {label} ---\n{_truncate(_answer_of(output), 2500)}\n" for label, output in slate
    )
    candidates = [label for label, _ in slate]
    user = (
        f"QUESTION:\n{prompt}\n\n"
        f"CANDIDATE ANSWERS ({len(slate)}):\n{blocks}\n"
        f"Rank all of: {', '.join(candidates)}"
    )

    async def job() -> dict:
        hub.publish(
            _sse_step(
                "step_start",
                {
                    "step_id": step_id,
                    "round": 0,
                    "phase": "vote",
                    "lane_id": voter["lane_id"],
                    "model": voter["model"],
                },
            )
        )
        return await _run_step(
            run_id=run_id,
            lane_id=voter["lane_id"],
            model=voter["model"],
            provider_id=voter["provider_id"],
            round_index=0,
            phase="vote",
            label=None,
            system=VOTE_SYSTEM,
            user=user,
            schema=VOTE_SCHEMA,
            required=("ranking",),
            validate=_validate_vote(candidates),
            hub=hub,
            step_id=step_id,
        )

    return job


async def _run_vote(
    hub: _Hub, run_id: str, panel: list[dict], prompt: str, outputs: dict[str, dict]
) -> tuple[dict, int]:
    """Hold a Borda vote over the current answers. Returns (result, calls made)."""
    entries = [p for p in panel if outputs.get(p["lane_id"])]
    if len(entries) < 2:
        return {}, 0
    order = list(entries)
    random.shuffle(order)
    slate = [(f"Answer {chr(65 + i)}", outputs[p["lane_id"]]) for i, p in enumerate(order)]
    label_to_lane = {f"Answer {chr(65 + i)}": p["lane_id"] for i, p in enumerate(order)}

    hub.publish(_sse_step("vote_start", {"candidates": list(label_to_lane)}))
    results = await _fan_out(
        [
            _vote_job(hub, run_id, voter, prompt, slate, f"{run_id}:vote:{voter['lane_id']}")
            for voter in entries
        ],
        settings.DELIBERATION_CONCURRENCY,
    )

    ballots: dict[str, list[str]] = {}
    for voter, result in zip(entries, results):
        if result.get("ok"):
            ranking = result["output"].get("ranking") or []
            ballots[voter["lane_id"]] = [str(r) for r in ranking if isinstance(r, str)]
    if not ballots:
        return {}, len(results)

    ordered, firsts = borda_count(ballots, list(label_to_lane))
    ranking = [
        {
            "lane_id": label_to_lane[label],
            "label": label,
            "score": score,
            "first_place_votes": firsts.get(label, 0),
        }
        for label, score in ordered
    ]
    winner = ranking[0] if ranking else None
    payload = {
        "ranking": ranking,
        "winner_lane_id": winner["lane_id"] if winner else None,
        "ballots": {k: v for k, v in ballots.items()},
        "voters": len(ballots),
    }
    hub.publish(_sse_step("vote_done", payload))
    return payload, len(results)


async def _drive(run_id: str, hub: _Hub) -> None:
    """Run one deliberation to completion, publishing SSE frames as it goes."""
    started = time.monotonic()
    cancel = _cancels.get(run_id) or asyncio.Event()
    context = _load_context(run_id)
    if context is None:
        hub.publish(_sse_step("delib_error", {"detail": "run not found"}))
        hub.finish()
        return

    prompt: str = context["prompt"]
    config: dict = context["config"]
    panel: list[dict] = context["panel"]
    judge: dict | None = context["judge"]
    max_rounds = max(
        1,
        min(int(config.get("max_rounds") or settings.DELIBERATION_DEFAULT_ROUNDS),
            settings.DELIBERATION_MAX_ROUNDS),
    )
    # "quick" is draft + vote only: the majority-vote baseline that debate has to beat.
    quick = str(config.get("mode") or "council").lower() == "quick"
    evidence = bool(config.get("evidence"))
    concurrency = settings.DELIBERATION_CONCURRENCY
    lane_ids = [p["lane_id"] for p in panel]
    calls = 0

    hub.publish(
        _sse_step(
            "delib_start",
            {
                "run_id": run_id,
                "prompt": prompt,
                "max_rounds": max_rounds,
                "participants": panel,
                "judge": judge,
                "config": config,
            },
        )
    )
    _save(run_id, status="running")
    _set_lane_states(lane_ids, "streaming")

    outputs: dict[str, dict] = {}
    round_history: list[dict[str, dict]] = []
    traces: list[dict] = []
    status = "failed"

    try:
        # ---- Round 0: blind drafts -------------------------------------------------
        hub.publish(_sse_step("round_start", {"round": 0, "phase": "draft"}))
        draft_ids = {p["lane_id"]: f"{run_id}:0:{p['lane_id']}" for p in panel}
        results = await _fan_out(
            [_draft_job(hub, run_id, p, prompt, draft_ids[p["lane_id"]], evidence) for p in panel],
            concurrency,
        )
        calls += len(results)
        for participant, result in zip(panel, results):
            if result.get("ok"):
                outputs[participant["lane_id"]] = result["output"]
        # The barrier: nothing is released into round 1 until every draft is in.
        hub.publish(
            _sse_step("round_done", {"round": 0, "responded": list(outputs.keys())})
        )
        if not outputs:
            raise RuntimeError("no panelist produced an answer")
        round_history.append(dict(outputs))

        # ---- Quick mode: vote on the drafts and stop --------------------------------
        # No critique rounds. This is the cheap arm, and the one everything else is
        # measured against.
        if quick:
            vote, vote_calls = await _run_vote(hub, run_id, panel, prompt, outputs)
            calls += vote_calls
            if vote:
                _save(run_id, vote_json=vote)
                winner = vote.get("winner_lane_id")
                if winner and outputs.get(winner):
                    synthesis_text = _answer_of(outputs[winner])
                    _save(run_id, synthesis=synthesis_text)
                    hub.publish(
                        _sse_step(
                            "synthesis_done",
                            {
                                "answer": synthesis_text,
                                "minority_report": None,
                                "do_now": [],
                                "consider_later": [],
                                "skip": [],
                                "critique": None,
                                "source": "vote",
                            },
                        )
                    )
            status = "voted"
            raise _Finished

        # ---- Rounds 1..N: peer review ----------------------------------------------
        round_index = 0
        while round_index < max_rounds:
            if cancel.is_set():
                status = "stopped"
                break
            if (time.monotonic() - started) * 1000 > settings.DELIBERATION_WALL_CLOCK_MS:
                hub.publish(_sse_step("delib_notice", {"detail": "wall-clock budget reached"}))
                break

            round_index += 1
            hub.publish(_sse_step("round_start", {"round": round_index, "phase": "critique"}))

            jobs = []
            label_maps: dict[str, dict[str, str]] = {}
            active = [p for p in panel if outputs.get(p["lane_id"])]
            for participant in active:
                peers, label_to_lane = _assign_peers(participant, panel, outputs)
                if not peers:
                    continue
                label_maps[participant["lane_id"]] = label_to_lane
                jobs.append(
                    _critique_job(
                        hub,
                        run_id,
                        participant,
                        prompt,
                        outputs[participant["lane_id"]],
                        peers,
                        round_index,
                        f"{run_id}:{round_index}:{participant['lane_id']}",
                    )
                )
            if not jobs:
                break

            results = await _fan_out(jobs, concurrency)
            calls += len(results)

            previous = dict(outputs)
            responded: list[str] = []
            verdicts: dict[str, str] = {}
            round_outputs: dict[str, dict] = {}
            job_participants = [p for p in active if p["lane_id"] in label_maps]
            for participant, result in zip(job_participants, results):
                lane_id = participant["lane_id"]
                if not result.get("ok"):
                    # An errored panelist leaves the denominator rather than blocking the
                    # round; its previous position is carried forward unchanged.
                    round_outputs[lane_id] = previous.get(lane_id, {})
                    continue
                translated = _translate_peers(result["output"], label_maps[lane_id])
                round_outputs[lane_id] = translated
                outputs[lane_id] = translated
                responded.append(lane_id)
                verdicts[lane_id] = result.get("verdict") or REQUEST_CHANGES

            round_history.append(dict(round_outputs))
            trace = score_round(round_index, round_outputs, verdicts, previous, responded)
            traces.append(trace)
            hub.publish(_sse_step("convergence", trace))
            hub.publish(
                _sse_step("round_done", {"round": round_index, "responded": responded})
            )
            _save(run_id, convergence_json=traces, rounds_used=round_index, total_calls=calls)

            keep_going, why = should_continue(traces, round_index, max_rounds)
            hub.publish(
                _sse_step(
                    "round_decision",
                    {"round": round_index, "continue": keep_going, "reason": why},
                )
            )
            if not keep_going:
                break

        converged = bool(traces and traces[-1]["converged"])
        if status != "stopped":
            status = "converged" if converged else "no_consensus"

        # ---- Metrics ---------------------------------------------------------------
        metrics = panel_metrics(round_history[1:], traces)
        _save(run_id, metrics_json=metrics, converged=converged)
        hub.publish(_sse_step("metrics", metrics))

        # ---- Synthesis -------------------------------------------------------------
        synthesis_text = ""
        # A user who pressed Stop meant it: don't spend three more calls on a synthesis
        # they didn't wait for. Every draft is already persisted and visible.
        if config.get("synthesis", True) and outputs and status != "stopped":
            synthesizer = _pick_synthesizer(panel, judge, metrics.get("influence", {}))
            hub.publish(
                _sse_step(
                    "synthesis_start",
                    {
                        "lane_id": synthesizer.get("lane_id"),
                        "model": synthesizer["model"],
                        "role": synthesizer.get("role", "runner-up"),
                    },
                )
            )
            synth = await _synthesize(hub, run_id, synthesizer, prompt, panel, outputs, traces)
            calls += 1
            if synth:
                synthesis_text = str(synth.get("answer") or "")
                critique = None
                if config.get("critique_synthesis", True):
                    critic = _pick_critic(panel, synthesizer)
                    if critic:
                        critique = await _critique_synthesis(
                            hub, run_id, critic, prompt, synthesis_text, outputs
                        )
                        calls += 1
                        if critique and not critique.get("faithful") and critique.get("issues"):
                            revised = await _revise_synthesis(
                                hub, run_id, synthesizer, prompt, synth, critique
                            )
                            calls += 1
                            if revised:
                                synth = revised
                                synthesis_text = str(synth.get("answer") or synthesis_text)
                _save(
                    run_id,
                    synthesis=synthesis_text,
                    minority_report=str(synth.get("minority_report") or "") or None,
                    extraction_json={
                        "do_now": synth.get("do_now") or [],
                        "consider_later": synth.get("consider_later") or [],
                        "skip": synth.get("skip") or [],
                    },
                    synthesis_critique_json=critique or {},
                )
                hub.publish(
                    _sse_step(
                        "synthesis_done",
                        {
                            "answer": synthesis_text,
                            "minority_report": synth.get("minority_report"),
                            "do_now": synth.get("do_now") or [],
                            "consider_later": synth.get("consider_later") or [],
                            "skip": synth.get("skip") or [],
                            "critique": critique,
                        },
                    )
                )
    except _Finished:
        pass
    except Exception as exc:  # noqa: BLE001
        status = "failed"
        _save(run_id, error=str(exc))
        hub.publish(_sse_step("delib_error", {"detail": str(exc)}))
    finally:
        wall = int((time.monotonic() - started) * 1000)
        _save(
            run_id,
            status=status,
            rounds_used=len(traces),
            total_calls=calls,
            wall_ms=wall,
            convergence_json=traces,
        )
        _set_lane_states(lane_ids, "idle")
        hub.publish(
            _sse_step(
                "delib_done",
                {
                    "run_id": run_id,
                    "status": status,
                    "rounds_used": len(traces),
                    "converged": bool(traces and traces[-1]["converged"]),
                    "total_calls": calls,
                    "wall_ms": wall,
                },
            )
        )
        hub.finish()
        _schedule_cleanup(run_id)


def _pick_synthesizer(panel: list[dict], judge: dict | None, influence: dict) -> dict:
    """Choose who writes the final answer.

    Preference order: a dedicated judge lane that is *not* on the panel (a judge drawn from
    the panel tends to favour its own contributions), otherwise the panel's runner-up.
    Never the leader — the model whose claims dominated has the least incentive to
    represent the dissent fairly.
    """
    panel_models = {p["model"] for p in panel}
    if judge and judge["model"] not in panel_models:
        return {**judge, "role": "independent judge"}
    if len(panel) == 1:
        return {**panel[0], "role": "sole panelist"}
    ranked = sorted(panel, key=lambda p: influence.get(p["lane_id"], 0.0), reverse=True)
    return {**ranked[1], "role": "runner-up"}


def _pick_critic(panel: list[dict], synthesizer: dict) -> dict | None:
    """A different voice audits the synthesis — never the model that wrote it."""
    for participant in panel:
        if participant["lane_id"] != synthesizer.get("lane_id") and participant["model"] != synthesizer["model"]:
            return participant
    return None


def _final_positions(panel: list[dict], outputs: dict[str, dict]) -> str:
    parts = []
    for index, participant in enumerate(panel):
        output = outputs.get(participant["lane_id"])
        if not output:
            continue
        label = _LABELS[index % len(_LABELS)]
        parts.append(
            f"--- {label} (final position) ---\n{_truncate(_answer_of(output))}\n\n"
            f"{label} CLAIMS:\n{_claim_lines(output, label.split()[-1])}\n"
        )
    return "\n".join(parts)


def _unresolved_block(traces: list[dict]) -> str:
    if not traces:
        return "(none recorded)"
    objections = traces[-1].get("open_objections") or []
    if not objections:
        return "(none — the panel resolved every objection)"
    return "\n".join(
        f"- {o.get('reason')} (raised against claim {o.get('claim_id')})" for o in objections
    )


async def _synthesize(
    hub: _Hub,
    run_id: str,
    synthesizer: dict,
    prompt: str,
    panel: list[dict],
    outputs: dict[str, dict],
    traces: list[dict],
) -> dict | None:
    user = (
        f"QUESTION:\n{prompt}\n\n"
        f"FINAL PANEL POSITIONS:\n{_final_positions(panel, outputs)}\n\n"
        f"UNRESOLVED OBJECTIONS:\n{_unresolved_block(traces)}"
    )
    result = await _run_step(
        run_id=run_id,
        lane_id=synthesizer.get("lane_id"),
        model=synthesizer["model"],
        provider_id=synthesizer["provider_id"],
        round_index=99,
        phase="synthesis",
        label=synthesizer.get("role"),
        system=SYNTHESIS_SYSTEM,
        user=user,
        schema=SYNTHESIS_SCHEMA,
        required=("answer",),
        validate=None,
        hub=hub,
        step_id=f"{run_id}:synthesis",
    )
    return result["output"] if result.get("ok") else None


async def _critique_synthesis(
    hub: _Hub, run_id: str, critic: dict, prompt: str, synthesis: str, outputs: dict[str, dict]
) -> dict | None:
    user = (
        f"QUESTION:\n{prompt}\n\n"
        f"SYNTHESIS UNDER REVIEW:\n{_truncate(synthesis, 6000)}\n\n"
        f"WHAT THE PANEL ACTUALLY CONCLUDED:\n"
        + "\n".join(_truncate(_answer_of(o), 1500) for o in outputs.values())
    )
    result = await _run_step(
        run_id=run_id,
        lane_id=critic["lane_id"],
        model=critic["model"],
        provider_id=critic["provider_id"],
        round_index=99,
        phase="synthesis_critique",
        label="auditor",
        system=SYNTHESIS_CRITIQUE_SYSTEM,
        user=user,
        schema=SYNTHESIS_CRITIQUE_SCHEMA,
        required=("faithful",),
        validate=None,
        hub=hub,
        step_id=f"{run_id}:synthesis_critique",
    )
    return result["output"] if result.get("ok") else None


async def _revise_synthesis(
    hub: _Hub, run_id: str, synthesizer: dict, prompt: str, synthesis: dict, critique: dict
) -> dict | None:
    issues = "\n".join(
        f"- [{i.get('severity', 'medium')}] {i.get('text')}"
        for i in (critique.get("issues") or [])
        if isinstance(i, dict)
    )
    user = (
        f"QUESTION:\n{prompt}\n\n"
        f"YOUR SYNTHESIS:\n{_truncate(str(synthesis.get('answer') or ''), 6000)}\n\n"
        f"MINORITY REPORT YOU WROTE:\n{synthesis.get('minority_report') or '(none)'}\n\n"
        f"AUDITOR ISSUES:\n{issues}"
    )
    result = await _run_step(
        run_id=run_id,
        lane_id=synthesizer.get("lane_id"),
        model=synthesizer["model"],
        provider_id=synthesizer["provider_id"],
        round_index=99,
        phase="synthesis_revise",
        label=synthesizer.get("role"),
        system=SYNTHESIS_REVISE_SYSTEM,
        user=user,
        schema=SYNTHESIS_SCHEMA,
        required=("answer",),
        validate=None,
        hub=hub,
        step_id=f"{run_id}:synthesis_revise",
    )
    return result["output"] if result.get("ok") else None
