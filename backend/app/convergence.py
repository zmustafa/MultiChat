"""Decide when a panel has stopped disagreeing — and prove it.

Convergence is judged on **verdicts first**: a round converges only when every peer that
actually answered says APPROVE and no objection is left open. Text similarity is reported
alongside as a stability signal, never as the gate — two answers can be worded differently
and mean the same thing, or read alike and both be wrong.

Everything here is deliberately cheap (token sets, no embeddings) so a round's score costs
nothing next to the model calls it summarizes.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

APPROVE = "APPROVE"
REQUEST_CHANGES = "REQUEST_CHANGES"
REJECT = "REJECT"
VERDICTS = (APPROVE, REQUEST_CHANGES, REJECT)

# Words carrying no discriminative signal when comparing claims.
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "could", "for", "from",
    "has", "have", "in", "is", "it", "its", "may", "must", "not", "of", "on", "or", "should",
    "that", "the", "then", "there", "these", "this", "to", "use", "using", "was", "were",
    "will", "with", "would", "you", "your",
}
_WORD_RE = re.compile(r"[a-z]+|[0-9]+(?:\.[0-9]+)?")

# A round's claims must move less than this for the panel to count as settled.
DELTA_THRESHOLD = 0.15
# Mean pairwise claim overlap above this counts as the panel having converged on wording.
OVERLAP_THRESHOLD = 0.45


def normalize(text: str) -> set[str]:
    """Reduce a claim to the token set that carries its meaning.

    Splits on punctuation so that ``0.5-1.0 FTE`` and ``0.5 to 1.0 FTE`` share tokens —
    models phrase the same number three different ways.
    """
    return {w for w in _WORD_RE.findall((text or "").lower()) if w not in _STOPWORDS and len(w) > 1}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def containment(a: set[str], b: set[str]) -> float:
    """Overlap relative to the smaller set — forgiving of one model simply saying more."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def token_similarity(a: set[str], b: set[str]) -> float:
    return 0.5 * jaccard(a, b) + 0.5 * containment(a, b)


def claim_texts(output: dict | None) -> list[str]:
    """The claim strings from a step output, tolerating degraded shapes."""
    if not output:
        return []
    claims = output.get("claims")
    if isinstance(claims, list):
        texts = []
        for c in claims:
            if isinstance(c, dict):
                text = c.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text)
            elif isinstance(c, str) and c.strip():
                texts.append(c)
        if texts:
            return texts
    # Degraded output (no parseable claims): fall back to the answer body so a stubborn
    # model still contributes to the similarity signal instead of scoring zero.
    body = output.get("revised_answer") or output.get("answer") or ""
    return [body] if isinstance(body, str) and body.strip() else []


def claim_similarity(left: Iterable[str], right: Iterable[str]) -> float:
    """Mean best-match overlap between two claim sets (symmetric).

    This is **lexical**, not semantic: two models can make identical points in different
    words and still score low. It is reliable for comparing a model against its *own*
    previous round (same voice, same phrasing) and for shading a relative heatmap — it is
    not a trustworthy absolute measure of whether a panel agrees. Verdicts are used for
    that.
    """
    a = [normalize(t) for t in left]
    b = [normalize(t) for t in right]
    if not a or not b:
        return 0.0

    def directed(src: list[set[str]], dst: list[set[str]]) -> float:
        return sum(max(token_similarity(s, d) for d in dst) for s in src) / len(src)

    return (directed(a, b) + directed(b, a)) / 2


def overlap_matrix(outputs: dict[str, dict]) -> tuple[list[str], list[list[float]]]:
    """Pairwise claim overlap between participants — the herding detector.

    Rendered as a heatmap it shows whether the panel is genuinely spread out or has
    collapsed onto one position. Read the shading relatively, not as a percentage.
    """
    labels = list(outputs.keys())
    claims = {k: claim_texts(v) for k, v in outputs.items()}
    matrix: list[list[float]] = []
    for row in labels:
        matrix.append(
            [1.0 if row == col else round(claim_similarity(claims[row], claims[col]), 3)
             for col in labels]
        )
    return labels, matrix


def mean_overlap(labels: list[str], matrix: list[list[float]]) -> float:
    pairs = [
        matrix[i][j]
        for i in range(len(labels))
        for j in range(i + 1, len(labels))
    ]
    return round(sum(pairs) / len(pairs), 3) if pairs else 1.0


def self_deltas(current: dict[str, dict], previous: dict[str, dict]) -> dict[str, float]:
    """How far each participant moved from its own previous position."""
    out: dict[str, float] = {}
    for key, now in current.items():
        before = previous.get(key)
        if not before:
            continue
        out[key] = round(1.0 - claim_similarity(claim_texts(now), claim_texts(before)), 3)
    return out


def open_objections(outputs: dict[str, dict]) -> list[dict]:
    """Rejections raised this round, i.e. disagreements still on the table."""
    found: list[dict] = []
    for key, output in outputs.items():
        for item in output.get("rejected_claims") or []:
            if not isinstance(item, dict):
                continue
            found.append(
                {
                    "by": key,
                    "peer": item.get("peer"),
                    "claim_id": item.get("claim_id"),
                    "reason": item.get("reason"),
                }
            )
    return found


def score_round(
    round_index: int,
    outputs: dict[str, dict],
    verdicts: dict[str, str],
    previous: dict[str, dict] | None,
    responded: list[str],
) -> dict:
    """Summarize one round: did the panel settle, and on what evidence?

    Two different measures, deliberately not conflated:

    * ``agreement`` — the share of responding peers that returned APPROVE. This is what
      the gate uses and what the UI shows, because it is what the panel actually said.
    * ``claim_overlap`` — lexical similarity between claim sets. Useful for spotting
      herding and for round-over-round drift, but it cannot see two models making the
      same point in different words, so it is never presented as "agreement".

    ``responded`` lists the participants that returned a usable answer — a model that
    errored leaves the denominator rather than blocking the round forever.
    """
    labels, matrix = overlap_matrix(outputs)
    overlap = mean_overlap(labels, matrix)
    deltas = self_deltas(outputs, previous or {})
    objections = open_objections(outputs)

    approvals = [p for p in responded if verdicts.get(p) == APPROVE]
    # Guard: convergence needs someone to have actually answered, and every one of those
    # who did must approve. No single participant can carry a round on its own approval.
    all_approve = bool(responded) and len(approvals) == len(responded)
    agreement = round(len(approvals) / len(responded), 3) if responded else 0.0
    settled = not deltas or max(deltas.values()) < DELTA_THRESHOLD

    converged = all_approve and not objections
    return {
        "round": round_index,
        "agreement": agreement,
        "claim_overlap": overlap,
        "diversity": round(1.0 - overlap, 3),
        "aligned": overlap >= OVERLAP_THRESHOLD,
        "labels": labels,
        "matrix": matrix,
        "self_deltas": deltas,
        "stable": settled,
        "verdicts": {k: verdicts.get(k) for k in outputs},
        "responded": responded,
        "approvals": approvals,
        "open_objections": objections,
        "open_objection_count": len(objections),
        "converged": converged,
    }


def should_continue(
    traces: list[dict], round_index: int, max_rounds: int
) -> tuple[bool, str]:
    """Decide whether to run another round, and say why in words a user can read."""
    if not traces:
        return True, "no rounds scored yet"
    last = traces[-1]
    if last["converged"]:
        return False, "every responding peer approved with no open objections"
    if round_index >= max_rounds:
        return False, f"reached the {max_rounds}-round limit without consensus"
    if len(traces) >= 3:
        a, b, c = (t["claim_overlap"] for t in traces[-3:])
        if c < b < a:
            # Further rounds won't help: the panel is diverging, not converging.
            return False, "positions moved further apart two rounds running — real disagreement"
    return True, f"{last['open_objection_count']} objection(s) still unresolved"


def panel_metrics(
    rounds: list[dict[str, dict]], traces: list[dict]
) -> dict:
    """Per-model behaviour across the whole run — who was persuasive, who just folded.

    * influence: share of accepted-claim references pointing at that participant.
    * capitulation: how often it changed position without naming what changed its mind.
    """
    influence: dict[str, int] = {}
    changes: dict[str, list[bool]] = {}
    for round_outputs in rounds:
        for key, output in round_outputs.items():
            for item in output.get("accepted_claims") or []:
                if isinstance(item, dict) and item.get("peer"):
                    influence[str(item["peer"])] = influence.get(str(item["peer"]), 0) + 1
            if output.get("position_changed") is not None:
                unjustified = bool(output.get("position_changed")) and not (
                    output.get("change_trigger") or ""
                ).strip()
                changes.setdefault(key, []).append(unjustified)

    total = sum(influence.values()) or 1
    return {
        "influence": {k: round(v / total, 3) for k, v in influence.items()},
        "influence_counts": influence,
        "capitulation": {
            k: round(sum(1 for x in v if x) / len(v), 3) for k, v in changes.items() if v
        },
        "final_agreement": traces[-1]["agreement"] if traces else None,
        "final_overlap": traces[-1]["claim_overlap"] if traces else None,
        "final_diversity": traces[-1]["diversity"] if traces else None,
    }


# ---------------------------------------------------------------------------
# Voting — the baseline the whole protocol has to beat
# ---------------------------------------------------------------------------


def borda_count(
    ballots: dict[str, list[str]], candidates: list[str]
) -> tuple[list[tuple[str, int]], dict[str, int]]:
    """Aggregate ranked ballots by Borda count.

    Most of the measured benefit of multi-model setups comes from plain voting rather than
    from the debate that follows it, so this is not a lesser mode — it is the yardstick.
    Each ballot ranks candidates best-first; a candidate scores ``n-1`` for a first place,
    ``n-2`` for a second, and so on. Ties break on the number of first-place votes.

    Returns ``(ranking, scores)`` where ranking is sorted best-first.
    """
    n = len(candidates)
    scores = {c: 0 for c in candidates}
    firsts = {c: 0 for c in candidates}
    for ranking in ballots.values():
        seen: set[str] = set()
        position = 0
        for candidate in ranking:
            if candidate not in scores or candidate in seen:
                continue  # ignore unknown or repeated entries rather than failing the vote
            seen.add(candidate)
            scores[candidate] += n - 1 - position
            if position == 0:
                firsts[candidate] += 1
            position += 1
    ordered = sorted(scores.items(), key=lambda kv: (kv[1], firsts[kv[0]]), reverse=True)
    return ordered, firsts
