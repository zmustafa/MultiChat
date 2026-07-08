from __future__ import annotations

import asyncio
import json
import re
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..db import SessionLocal, get_db
from ..models import EvalRun, EvalSuite, Provider, User
from ..providers.registry import build_provider, pick_default_provider
from ..security import current_user

router = APIRouter(prefix="/api/evals", tags=["evals"])

# Max number of eval cells (prompt × model) to run concurrently.
EVAL_CONCURRENCY = 5


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


class EvalModelRef(BaseModel):
    provider_id: str
    model: str


class SuiteIn(BaseModel):
    name: str
    description: str | None = None
    system_prompt: str | None = None
    prompts: list[str] = []
    models: list[EvalModelRef] = []


class SuiteOut(SuiteIn):
    id: str
    created_at: str
    updated_at: str


def _serialize(s: EvalSuite) -> SuiteOut:
    return SuiteOut(
        id=s.id,
        name=s.name,
        description=s.description,
        system_prompt=s.system_prompt,
        prompts=s.prompts_json or [],
        models=[EvalModelRef(**m) for m in (s.models_json or [])],
        created_at=s.created_at.isoformat(),
        updated_at=s.updated_at.isoformat(),
    )


def _get_owned(db: DbSession, user: User, suite_id: str) -> EvalSuite:
    s = db.get(EvalSuite, suite_id)
    if not s or s.user_id != user.id:
        raise HTTPException(status_code=404, detail="Suite not found")
    return s


@router.get("", response_model=list[SuiteOut])
def list_suites(user: User = Depends(current_user), db: DbSession = Depends(get_db)):
    rows = db.scalars(
        select(EvalSuite).where(EvalSuite.user_id == user.id).order_by(EvalSuite.name)
    ).all()
    return [_serialize(s) for s in rows]


@router.post("", response_model=SuiteOut)
def create_suite(
    payload: SuiteIn,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
):
    s = EvalSuite(
        user_id=user.id,
        name=payload.name.strip() or "Untitled suite",
        description=payload.description,
        system_prompt=payload.system_prompt,
        prompts_json=[p for p in payload.prompts if p.strip()],
        models_json=[m.model_dump() for m in payload.models],
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return _serialize(s)


@router.patch("/{suite_id}", response_model=SuiteOut)
def update_suite(
    suite_id: str,
    payload: SuiteIn,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
):
    s = _get_owned(db, user, suite_id)
    s.name = payload.name.strip() or s.name
    s.description = payload.description
    s.system_prompt = payload.system_prompt
    s.prompts_json = [p for p in payload.prompts if p.strip()]
    s.models_json = [m.model_dump() for m in payload.models]
    db.commit()
    db.refresh(s)
    return _serialize(s)


@router.delete("/{suite_id}", status_code=204)
def delete_suite(
    suite_id: str,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
):
    s = _get_owned(db, user, suite_id)
    db.delete(s)
    db.commit()


async def _score_answer(
    prov: Provider, db: DbSession, model: str, prompt: str, answer: str
) -> int | None:
    """Ask the judge model to score an answer 1-10; returns None on failure."""
    system = (
        "You are a strict evaluator. Score how well the ANSWER addresses the PROMPT on a "
        "scale of 1 to 10 (10 = excellent, correct, complete, well-structured). Reply with "
        "ONLY the integer."
    )
    user_msg = f"PROMPT:\n{prompt[:2000]}\n\nANSWER:\n{answer[:4000]}\n\nScore (1-10):"
    try:
        llm = await build_provider(prov, db, model)
        text = ""
        async for ev in llm.stream(
            [{"role": "system", "content": system}, {"role": "user", "content": user_msg}],
            None,
        ):
            if ev.type == "token" and ev.text:
                text += ev.text
        match = re.search(r"\d+", text)
        if match:
            return max(1, min(10, int(match.group())))
    except Exception:  # noqa: BLE001
        return None
    return None


def _build_summary(results: list[dict]) -> dict:
    by_model: dict[str, dict] = {}
    for r in results:
        agg = by_model.setdefault(
            r["model"],
            {
                "count": 0,
                "score_sum": 0,
                "score_n": 0,
                "lat_sum": 0,
                "tps_sum": 0,
                "tps_n": 0,
                "ttft_sum": 0,
                "ttft_n": 0,
            },
        )
        agg["count"] += 1
        agg["lat_sum"] += r["latency_ms"]
        if r["score"] is not None:
            agg["score_sum"] += r["score"]
            agg["score_n"] += 1
        if r.get("tps") is not None:
            agg["tps_sum"] += r["tps"]
            agg["tps_n"] += 1
        if r.get("ttft_ms") is not None:
            agg["ttft_sum"] += r["ttft_ms"]
            agg["ttft_n"] += 1
    return {
        m: {
            "avg_score": round(a["score_sum"] / a["score_n"], 2) if a["score_n"] else None,
            "avg_latency_ms": round(a["lat_sum"] / a["count"]) if a["count"] else None,
            "avg_tps": round(a["tps_sum"] / a["tps_n"], 1) if a["tps_n"] else None,
            "avg_ttft_ms": round(a["ttft_sum"] / a["ttft_n"]) if a["ttft_n"] else None,
            "count": a["count"],
        }
        for m, a in by_model.items()
    }


@router.post("/{suite_id}/run")
async def run_suite(
    suite_id: str,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> dict:
    """Run every prompt against every configured model, score each answer with the
    default (judge) provider, and persist an EvalRun."""
    suite = _get_owned(db, user, suite_id)
    prompts = suite.prompts_json or []
    models = suite.models_json or []
    if not prompts or not models:
        raise HTTPException(status_code=400, detail="Add prompts and models first")

    judge = pick_default_provider(db, user.id)
    judge_model = (
        (judge.default_model or ((judge.models_json or [""])[0] if judge.models_json else ""))
        if judge
        else ""
    )

    results: list[dict] = []
    for prompt in prompts:
        for mref in models:
            prov = db.get(Provider, mref.get("provider_id"))
            model = mref.get("model", "")
            if not prov or prov.user_id != user.id or not model:
                continue
            msgs = []
            if suite.system_prompt:
                msgs.append({"role": "system", "content": suite.system_prompt})
            msgs.append({"role": "user", "content": prompt})
            started = time.monotonic()
            text = ""
            ctokens = 0
            ttft = None
            err = None
            try:
                llm = await build_provider(prov, db, model)
                async for ev in llm.stream(msgs, None):
                    if ev.type == "token" and ev.text:
                        if ttft is None:
                            ttft = int((time.monotonic() - started) * 1000)
                        text += ev.text
                    elif ev.type == "done":
                        ctokens += ev.completion_tokens or 0
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
            latency = int((time.monotonic() - started) * 1000)
            score = None
            if not err and text and judge and judge_model:
                score = await _score_answer(judge, db, judge_model, prompt, text)
            tokens = ctokens or (len(text) // 4 if text else 0)
            tps = round(tokens / (latency / 1000), 1) if latency > 0 and tokens else None
            results.append(
                {
                    "prompt": prompt,
                    "model": model,
                    "provider": prov.name,
                    "answer": err and f"ERROR: {err}" or text,
                    "error": bool(err),
                    "score": score,
                    "latency_ms": latency,
                    "ttft_ms": ttft,
                    "tokens": tokens,
                    "tps": tps,
                }
            )

    by_model: dict[str, dict] = {}
    for r in results:
        agg = by_model.setdefault(
            r["model"],
            {
                "count": 0,
                "score_sum": 0,
                "score_n": 0,
                "lat_sum": 0,
                "tps_sum": 0,
                "tps_n": 0,
                "ttft_sum": 0,
                "ttft_n": 0,
            },
        )
        agg["count"] += 1
        agg["lat_sum"] += r["latency_ms"]
        if r["score"] is not None:
            agg["score_sum"] += r["score"]
            agg["score_n"] += 1
        if r.get("tps") is not None:
            agg["tps_sum"] += r["tps"]
            agg["tps_n"] += 1
        if r.get("ttft_ms") is not None:
            agg["ttft_sum"] += r["ttft_ms"]
            agg["ttft_n"] += 1
    summary = {
        m: {
            "avg_score": round(a["score_sum"] / a["score_n"], 2) if a["score_n"] else None,
            "avg_latency_ms": round(a["lat_sum"] / a["count"]) if a["count"] else None,
            "avg_tps": round(a["tps_sum"] / a["tps_n"], 1) if a["tps_n"] else None,
            "avg_ttft_ms": round(a["ttft_sum"] / a["ttft_n"]) if a["ttft_n"] else None,
            "count": a["count"],
        }
        for m, a in by_model.items()
    }

    run = EvalRun(
        suite_id=suite.id,
        user_id=user.id,
        results_json=results,
        summary_json=summary,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return {
        "id": run.id,
        "created_at": run.created_at.isoformat(),
        "results": results,
        "summary": summary,
    }


@router.post("/{suite_id}/run/stream")
async def run_suite_stream(
    suite_id: str,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> StreamingResponse:
    """Same as /run but streams live progress (SSE): a run_start event, then per prompt×model
    cell events (cell_start, cell_token deltas, cell_answer, cell_scoring, cell_score), and a
    final `done` event carrying the persisted run's full results + summary."""
    suite = _get_owned(db, user, suite_id)
    prompts = suite.prompts_json or []
    models = suite.models_json or []
    if not prompts or not models:
        raise HTTPException(status_code=400, detail="Add prompts and models first")

    judge = pick_default_provider(db, user.id)
    judge_model = (
        (judge.default_model or ((judge.models_json or [""])[0] if judge.models_json else ""))
        if judge
        else ""
    )
    judge_id = judge.id if judge else None

    model_infos: list[tuple[str, str, str]] = []
    for mref in models:
        prov = db.get(Provider, mref.get("provider_id"))
        model = mref.get("model", "")
        if prov and prov.user_id == user.id and model:
            model_infos.append((prov.id, prov.name, model))

    system_prompt = suite.system_prompt

    # Build the full cell grid (prompt × model), preserving position so results stay ordered
    # even though cells finish out of order when run in parallel.
    cells: list[tuple[int, int, str, str, str, str]] = []
    for pi, prompt in enumerate(prompts):
        for provider_id, provider_name, model in model_infos:
            cells.append(
                (len(cells), pi, prompt, provider_id, provider_name, model)
            )

    async def gen():
        yield _sse(
            "run_start",
            {
                "prompts": prompts,
                "models": [{"model": m, "provider": n} for _, n, m in model_infos],
                "total": len(cells),
            },
        )

        results_arr: list[dict | None] = [None] * len(cells)
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        sem = asyncio.Semaphore(EVAL_CONCURRENCY)

        async def run_cell(
            idx: int,
            pi: int,
            prompt: str,
            provider_id: str,
            provider_name: str,
            model: str,
        ) -> None:
            async with sem:
                await queue.put(
                    _sse(
                        "cell_start",
                        {"prompt_index": pi, "model": model, "provider": provider_name},
                    )
                )
                cell_db = SessionLocal()
                try:
                    prov = cell_db.get(Provider, provider_id)
                    msgs = []
                    if system_prompt:
                        msgs.append({"role": "system", "content": system_prompt})
                    msgs.append({"role": "user", "content": prompt})
                    started = time.monotonic()
                    text = ""
                    ctokens = 0
                    ttft = None
                    err = None
                    try:
                        if not prov:
                            raise RuntimeError("Provider not found")
                        llm = await build_provider(prov, cell_db, model)
                        async for ev in llm.stream(msgs, None):
                            if ev.type == "token" and ev.text:
                                if ttft is None:
                                    ttft = int((time.monotonic() - started) * 1000)
                                text += ev.text
                                await queue.put(
                                    _sse(
                                        "cell_token",
                                        {"prompt_index": pi, "model": model, "delta": ev.text},
                                    )
                                )
                            elif ev.type == "done":
                                ctokens += ev.completion_tokens or 0
                    except Exception as exc:  # noqa: BLE001
                        err = str(exc)
                    latency = int((time.monotonic() - started) * 1000)
                    answer = f"ERROR: {err}" if err else text
                    tokens = ctokens or (len(text) // 4 if text else 0)
                    tps = round(tokens / (latency / 1000), 1) if latency > 0 and tokens else None
                    await queue.put(
                        _sse(
                            "cell_answer",
                            {
                                "prompt_index": pi,
                                "model": model,
                                "answer": answer,
                                "error": bool(err),
                                "latency_ms": latency,
                                "ttft_ms": ttft,
                                "tokens": tokens,
                                "tps": tps,
                            },
                        )
                    )
                    score = None
                    if not err and text and judge_id and judge_model:
                        await queue.put(
                            _sse("cell_scoring", {"prompt_index": pi, "model": model})
                        )
                        judge_prov = cell_db.get(Provider, judge_id)
                        if judge_prov:
                            score = await _score_answer(
                                judge_prov, cell_db, judge_model, prompt, text
                            )
                    await queue.put(
                        _sse(
                            "cell_score",
                            {"prompt_index": pi, "model": model, "score": score},
                        )
                    )
                    results_arr[idx] = {
                        "prompt": prompt,
                        "model": model,
                        "provider": provider_name,
                        "answer": answer,
                        "error": bool(err),
                        "score": score,
                        "latency_ms": latency,
                        "ttft_ms": ttft,
                        "tokens": tokens,
                        "tps": tps,
                    }
                finally:
                    cell_db.close()

        tasks = [asyncio.create_task(run_cell(*c)) for c in cells]

        async def _drain() -> None:
            await asyncio.gather(*tasks, return_exceptions=True)
            await queue.put(None)

        drainer = asyncio.create_task(_drain())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            await drainer

        results = [r for r in results_arr if r]
        summary = _build_summary(results)
        run = EvalRun(
            suite_id=suite.id,
            user_id=user.id,
            results_json=results,
            summary_json=summary,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        yield _sse(
            "done",
            {
                "id": run.id,
                "created_at": run.created_at.isoformat(),
                "results": results,
                "summary": summary,
            },
        )

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/{suite_id}/runs")
def list_runs(
    suite_id: str,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> list[dict]:
    _get_owned(db, user, suite_id)
    rows = db.scalars(
        select(EvalRun)
        .where(EvalRun.suite_id == suite_id, EvalRun.user_id == user.id)
        .order_by(EvalRun.created_at.desc())
        .limit(20)
    ).all()
    return [
        {
            "id": r.id,
            "created_at": r.created_at.isoformat(),
            "summary": r.summary_json or {},
            "results": r.results_json or [],
        }
        for r in rows
    ]
