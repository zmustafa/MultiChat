"""Phase 3: does deliberation actually beat the cheap alternatives?

Multi-model debate is expensive and the literature is blunt about it — most of the measured
benefit of a panel comes from plain voting, and iterating afterwards does not reliably
improve correctness. So the feature has to earn its cost against the alternatives on the
user's own prompts, not on a benchmark someone else published.

Four arms, all starting from the **same drafts** so the comparison is about what happens
after the first answer, not about draft luck:

* ``single``      — one model, one answer. The floor.
* ``vote``        — the panel ranks the drafts, Borda picks a winner. The real baseline.
* ``synthesize``  — one editor merges the drafts. What ``/synthesize`` already does today.
* ``council``     — full peer review then synthesis. The feature.

A judge that is not on the panel then scores all four blind, anonymized and shuffled, in a
single call so the scores are relative to each other rather than to an absolute scale.
"""
from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator
from typing import Any

from .broadcast import sse
from .config import settings
from .convergence import borda_count, score_round
from .db import SessionLocal
from .deliberation import (
    CRITIQUE_SCHEMA,
    CRITIQUE_SYSTEM,
    DRAFT_SCHEMA,
    DRAFT_SYSTEM,
    SYNTHESIS_SCHEMA,
    SYNTHESIS_SYSTEM,
    VOTE_SCHEMA,
    VOTE_SYSTEM,
    _answer_of,
    _claim_lines,
    _peer_block,
    _truncate,
    _validate_critique,
    _validate_vote,
)
from .models import Provider
from .structured import call_structured

ARMS = ("single", "vote", "synthesize", "council")

JUDGE_SYSTEM = (
    "You are scoring candidate answers to the same question.\n\n"
    "They are ANONYMIZED and shuffled; you do not know how any of them was produced and "
    "must not speculate. Score each from 1 to 10 on correctness, completeness, and whether "
    "the recommendation is concrete enough to act on.\n\n"
    "Be discriminating. If they differ in quality, the scores must differ — giving everything "
    "an 8 is a failed evaluation. Length is not quality: a shorter answer that commits beats "
    "a longer one that hedges."
)

JUDGE_SCHEMA = """{
  "scores": [{"label": "Answer A", "score": 7, "why": "one short sentence"}],
  "best": "Answer B"
}"""


async def _call(
    db, provider: Provider, model: str, *, system: str, user: str, schema: str, **kw
):
    return await call_structured(
        provider, db, model, system=system, user=user, schema=schema,
        repair_attempts=settings.DELIBERATION_REPAIR_ATTEMPTS, **kw
    )


async def _bounded(jobs: list[Any], concurrency: int) -> list[Any]:
    semaphore = asyncio.Semaphore(concurrency)

    async def guarded(job):
        async with semaphore:
            try:
                return await job()
            except Exception as exc:  # noqa: BLE001 — one arm failing must not stop the rest
                return {"error": str(exc)}

    return await asyncio.gather(*(guarded(j) for j in jobs))


def _panel_providers(db, panel: list[dict]) -> dict[str, Provider]:
    return {p["provider_id"]: db.get(Provider, p["provider_id"]) for p in panel}


async def _drafts(db, panel: list[dict], prompt: str, providers) -> tuple[dict, int]:
    async def job(member):
        async def run():
            result = await _call(
                db, providers[member["provider_id"]], member["model"],
                system=DRAFT_SYSTEM, user=f"QUESTION:\n{prompt}",
                schema=DRAFT_SCHEMA, required=("answer",),
            )
            return {"key": member["key"], "data": result.data, "degraded": result.degraded}

        return run

    results = await _bounded([await job(m) for m in panel], settings.DELIBERATION_CONCURRENCY)
    out = {}
    for r in results:
        if isinstance(r, dict) and r.get("key") and _answer_of(r.get("data") or {}):
            out[r["key"]] = r["data"]
    return out, len(results)


async def _vote_arm(db, panel, prompt, drafts, providers) -> tuple[str, int]:
    entries = [p for p in panel if drafts.get(p["key"])]
    if len(entries) < 2:
        return (_answer_of(drafts[entries[0]["key"]]) if entries else ""), 0
    order = list(entries)
    random.shuffle(order)
    slate = [(f"Answer {chr(65 + i)}", drafts[p["key"]]) for i, p in enumerate(order)]
    label_to_key = {f"Answer {chr(65 + i)}": p["key"] for i, p in enumerate(order)}
    blocks = "\n".join(
        f"--- {label} ---\n{_truncate(_answer_of(o), 2500)}\n" for label, o in slate
    )
    candidates = list(label_to_key)
    user = (
        f"QUESTION:\n{prompt}\n\nCANDIDATE ANSWERS ({len(slate)}):\n{blocks}\n"
        f"Rank all of: {', '.join(candidates)}"
    )

    async def job(member):
        async def run():
            result = await _call(
                db, providers[member["provider_id"]], member["model"],
                system=VOTE_SYSTEM, user=user, schema=VOTE_SCHEMA,
                required=("ranking",), validate=_validate_vote(candidates),
            )
            return {"key": member["key"], "ranking": result.data.get("ranking") or []}

        return run

    results = await _bounded([await job(m) for m in entries], settings.DELIBERATION_CONCURRENCY)
    ballots = {
        r["key"]: [str(x) for x in r["ranking"] if isinstance(x, str)]
        for r in results
        if isinstance(r, dict) and r.get("key")
    }
    if not ballots:
        return _answer_of(drafts[entries[0]["key"]]), len(results)
    ordered, _ = borda_count(ballots, candidates)
    winner_key = label_to_key.get(ordered[0][0]) if ordered else None
    return _answer_of(drafts.get(winner_key) or drafts[entries[0]["key"]]), len(results)


def _positions_block(panel, outputs) -> str:
    parts = []
    for index, member in enumerate(panel):
        output = outputs.get(member["key"])
        if not output:
            continue
        label = f"Peer {chr(65 + index)}"
        parts.append(
            f"--- {label} (final position) ---\n{_truncate(_answer_of(output))}\n\n"
            f"{label} CLAIMS:\n{_claim_lines(output, chr(65 + index))}\n"
        )
    return "\n".join(parts)


async def _synthesize_arm(
    db, editor: dict, prompt: str, panel, outputs, providers, unresolved: str
) -> tuple[str, int]:
    user = (
        f"QUESTION:\n{prompt}\n\nFINAL PANEL POSITIONS:\n{_positions_block(panel, outputs)}\n\n"
        f"UNRESOLVED OBJECTIONS:\n{unresolved}"
    )
    result = await _call(
        db, providers[editor["provider_id"]], editor["model"],
        system=SYNTHESIS_SYSTEM, user=user, schema=SYNTHESIS_SCHEMA, required=("answer",),
    )
    return str(result.data.get("answer") or ""), 1


async def _council_arm(
    db, panel, prompt, drafts, providers, max_rounds, editor
) -> tuple[str, int, list[dict]]:
    """Full peer review, then synthesis — the same protocol the product runs."""
    outputs = dict(drafts)
    traces: list[dict] = []
    calls = 0
    for round_index in range(1, max_rounds + 1):
        active = [p for p in panel if outputs.get(p["key"])]
        if len(active) < 2:
            break
        jobs = []
        maps: list[dict] = []
        for member in active:
            others = [p for p in active if p["key"] != member["key"]]
            random.shuffle(others)
            peers = [
                (f"Peer {chr(65 + i)}", chr(65 + i), outputs[p["key"]])
                for i, p in enumerate(others)
            ]
            maps.append({f"Peer {chr(65 + i)}": p["key"] for i, p in enumerate(others)})
            blocks = "\n".join(_peer_block(lbl, out, ltr) for lbl, ltr, out in peers)
            user = (
                f"QUESTION:\n{prompt}\n\n"
                f"YOUR PREVIOUS ANSWER:\n{_truncate(_answer_of(outputs[member['key']]))}\n\n"
                f"YOUR CLAIMS:\n{_claim_lines(outputs[member['key']], 'self')}\n\n"
                f"PEER ANSWERS (anonymized — {len(peers)} peer(s)):\n{blocks}"
            )

            async def run(m=member, u=user):
                result = await _call(
                    db, providers[m["provider_id"]], m["model"],
                    system=CRITIQUE_SYSTEM, user=u, schema=CRITIQUE_SCHEMA,
                    required=("verdict", "revised_answer"), validate=_validate_critique,
                )
                return {"key": m["key"], "data": result.data}

            jobs.append(run)

        results = await _bounded(jobs, settings.DELIBERATION_CONCURRENCY)
        calls += len(results)
        previous = dict(outputs)
        round_outputs: dict[str, dict] = {}
        verdicts: dict[str, str] = {}
        responded: list[str] = []
        for result, label_map in zip(results, maps):
            if not isinstance(result, dict) or not result.get("key"):
                continue
            key = result["key"]
            data = dict(result["data"])
            for field in ("accepted_claims", "rejected_claims"):
                items = data.get(field)
                if isinstance(items, list):
                    data[field] = [
                        {**i, "peer": label_map.get(str(i.get("peer")), i.get("peer"))}
                        if isinstance(i, dict)
                        else i
                        for i in items
                    ]
            outputs[key] = data
            round_outputs[key] = data
            verdicts[key] = str(data.get("verdict") or "").upper()
            responded.append(key)
        if not round_outputs:
            break
        trace = score_round(round_index, round_outputs, verdicts, previous, responded)
        traces.append(trace)
        if trace["converged"]:
            break

    unresolved = (
        "\n".join(f"- {o.get('reason')}" for o in (traces[-1]["open_objections"] if traces else []))
        or "(none)"
    )
    answer, synth_calls = await _synthesize_arm(
        db, editor, prompt, panel, outputs, providers, unresolved
    )
    return answer, calls + synth_calls, traces


async def _judge_arms(
    db, judge: dict, provider: Provider, prompt: str, answers: dict[str, str]
) -> tuple[dict, int]:
    """Score every arm blind, in one call, so the scores are relative to each other."""
    # Identical answers are scored once: two arms producing the same text must not get
    # different scores through judge noise.
    by_text: dict[str, list[str]] = {}
    for arm, text in answers.items():
        if text.strip():
            by_text.setdefault(text.strip(), []).append(arm)
    if not by_text:
        return {}, 0
    unique = list(by_text.items())
    random.shuffle(unique)
    labels = [f"Answer {chr(65 + i)}" for i in range(len(unique))]
    blocks = "\n".join(
        f"--- {labels[i]} ---\n{_truncate(text, 5000)}\n" for i, (text, _) in enumerate(unique)
    )
    user = f"QUESTION:\n{prompt}\n\nCANDIDATES:\n{blocks}\nScore every one of: {', '.join(labels)}"
    result = await _call(
        db, provider, judge["model"],
        system=JUDGE_SYSTEM, user=user, schema=JUDGE_SCHEMA, required=("scores",),
    )
    scored: dict[str, float] = {}
    reasons: dict[str, str] = {}
    for entry in result.data.get("scores") or []:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or "")
        if label not in labels:
            continue
        index = labels.index(label)
        try:
            value = max(1.0, min(10.0, float(entry.get("score"))))
        except (TypeError, ValueError):
            continue
        for arm in unique[index][1]:
            scored[arm] = value
            reasons[arm] = str(entry.get("why") or "")[:200]
    return {"scores": scored, "reasons": reasons}, 1


async def run_benchmark(
    prompts: list[str],
    panel: list[dict],
    judge: dict,
    max_rounds: int,
    arms: list[str],
) -> AsyncIterator[str]:
    """Stream a head-to-head comparison of the four arms over the given prompts."""
    started = time.monotonic()
    total_calls = 0
    results: list[dict] = []
    db = SessionLocal()
    try:
        providers = _panel_providers(db, panel + [judge])
        judge_provider = providers.get(judge["provider_id"])
        yield sse(
            "bench_start",
            {
                "prompts": len(prompts),
                "panel": [{"model": p["model"], "key": p["key"]} for p in panel],
                "judge": judge["model"],
                "arms": arms,
                "max_rounds": max_rounds,
            },
        )

        for index, prompt in enumerate(prompts):
            yield sse("prompt_start", {"index": index, "prompt": prompt[:200]})
            row: dict[str, Any] = {"prompt": prompt, "answers": {}, "scores": {}, "calls": 0}

            drafts, calls = await _drafts(db, panel, prompt, providers)
            row["calls"] += calls
            total_calls += calls
            if not drafts:
                row["error"] = "no drafts produced"
                results.append(row)
                yield sse("prompt_done", {"index": index, "result": row})
                continue
            yield sse("arm_done", {"index": index, "arm": "drafts", "n": len(drafts)})

            # single: the first panelist that actually answered
            first = next((p for p in panel if drafts.get(p["key"])), None)
            if "single" in arms and first:
                row["answers"]["single"] = _answer_of(drafts[first["key"]])
                yield sse("arm_done", {"index": index, "arm": "single"})

            if "vote" in arms:
                text, calls = await _vote_arm(db, panel, prompt, drafts, providers)
                row["answers"]["vote"] = text
                row["calls"] += calls
                total_calls += calls
                yield sse("arm_done", {"index": index, "arm": "vote"})

            if "synthesize" in arms:
                text, calls = await _synthesize_arm(
                    db, judge, prompt, panel, drafts, providers, "(no review was run)"
                )
                row["answers"]["synthesize"] = text
                row["calls"] += calls
                total_calls += calls
                yield sse("arm_done", {"index": index, "arm": "synthesize"})

            if "council" in arms:
                text, calls, traces = await _council_arm(
                    db, panel, prompt, drafts, providers, max_rounds, judge
                )
                row["answers"]["council"] = text
                row["calls"] += calls
                row["council_rounds"] = len(traces)
                row["council_converged"] = bool(traces and traces[-1]["converged"])
                total_calls += calls
                yield sse("arm_done", {"index": index, "arm": "council"})

            if judge_provider:
                yield sse("scoring", {"index": index})
                scored, calls = await _judge_arms(
                    db, judge, judge_provider, prompt, row["answers"]
                )
                row["scores"] = scored.get("scores", {})
                row["reasons"] = scored.get("reasons", {})
                row["calls"] += calls
                total_calls += calls

            results.append(row)
            yield sse(
                "prompt_done",
                {
                    "index": index,
                    "scores": row["scores"],
                    "calls": row["calls"],
                    "council_rounds": row.get("council_rounds"),
                },
            )

        summary = summarize(results, arms)
        yield sse(
            "bench_done",
            {
                "summary": summary,
                "results": [
                    {k: v for k, v in r.items() if k != "answers"} | {
                        "answers": {a: (t or "")[:4000] for a, t in (r.get("answers") or {}).items()}
                    }
                    for r in results
                ],
                "total_calls": total_calls,
                "wall_ms": int((time.monotonic() - started) * 1000),
            },
        )
    finally:
        db.close()


def summarize(results: list[dict], arms: list[str]) -> dict:
    """Average score per arm, plus the verdict the whole exercise exists to produce."""
    per_arm: dict[str, list[float]] = {a: [] for a in arms}
    wins: dict[str, int] = {a: 0 for a in arms}
    for row in results:
        scores = row.get("scores") or {}
        for arm, value in scores.items():
            if arm in per_arm:
                per_arm[arm].append(float(value))
        if scores:
            best = max(scores.items(), key=lambda kv: kv[1])[0]
            if best in wins:
                wins[best] += 1
    averages = {
        arm: (round(sum(v) / len(v), 2) if v else None) for arm, v in per_arm.items()
    }
    council = averages.get("council")
    vote = averages.get("vote")
    synth = averages.get("synthesize")
    single = averages.get("single")
    baseline = max([v for v in (vote, synth, single) if v is not None], default=None)

    if council is None or baseline is None:
        verdict = "inconclusive — not enough scored answers"
    elif council > baseline + 0.5:
        verdict = "deliberation wins — it beats the best cheap arm by a clear margin"
    elif council >= baseline - 0.25:
        verdict = "no better than the cheap arms — the extra calls are not buying quality"
    else:
        verdict = "deliberation is WORSE than the cheap arms on these prompts"

    return {
        "avg_scores": averages,
        "wins": wins,
        "prompts": len(results),
        "best_baseline": baseline,
        "council": council,
        "verdict": verdict,
    }
