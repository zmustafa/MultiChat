import { useMemo, useState } from "react";
import { useNavigate } from "react-router";
import { MessageRenderer } from "./MessageRenderer";
import { TextDiff } from "./TextDiff";
import { DeliberationAnalysis } from "./DeliberationAnalysis";
import { continueInChat, exportDeliberationPdf, stepAnswer, stopDeliberation, unsupportedFacts } from "../api/deliberation";
import type { ConvergenceTrace, DeliberationStep, VoteResult } from "../api/deliberation";
import { mergeSteps, useDeliberation } from "../hooks/useDeliberation";
import { mediaUrl } from "../api/client";

function VerdictChip({ verdict }: { verdict?: string | null }) {
  if (!verdict) return null;
  const map: Record<string, string> = {
    APPROVE: "bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300",
    REQUEST_CHANGES: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
    REJECT: "bg-rose-100 text-rose-700 dark:bg-rose-950 dark:text-rose-300",
  };
  const label =
    verdict === "APPROVE" ? "✓ Approve" : verdict === "REJECT" ? "✕ Reject" : "⚠ Changes";
  return (
    <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${map[verdict] ?? ""}`}>
      {label}
    </span>
  );
}

function Spinner() {
  return (
    <svg className="animate-spin" viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="2">
      <circle cx="12" cy="12" r="9" strokeOpacity="0.25" />
      <path d="M21 12a9 9 0 0 0-9-9" strokeLinecap="round" />
    </svg>
  );
}

/** One panelist's contribution in one round. */
function StepCard({
  step,
  model,
  previous,
  running,
  chars,
  errored,
}: {
  step?: DeliberationStep;
  model: string;
  previous?: DeliberationStep;
  running: boolean;
  chars: number;
  errored?: string;
}) {
  const [open, setOpen] = useState(false);
  const [showDiff, setShowDiff] = useState(false);
  const output = step?.output;
  const accepted = output?.accepted_claims?.length ?? 0;
  const rejected = output?.rejected_claims?.length ?? 0;
  const body = step ? stepAnswer(step) : "";

  return (
    <div className="flex min-w-0 flex-1 flex-col rounded-lg border border-gray-200 bg-white p-2 dark:border-gray-700 dark:bg-gray-900">
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-[11px] font-semibold text-gray-700 dark:text-gray-200">
          {model}
        </span>
        {running ? (
          <span className="flex items-center gap-1 text-[10px] text-gray-400">
            <Spinner />
            {chars > 0 ? `${chars} ch` : "thinking"}
          </span>
        ) : errored ? (
          <span className="text-[10px] text-rose-500">failed</span>
        ) : (
          <VerdictChip verdict={step?.verdict} />
        )}
      </div>

      {errored ? (
        <p className="mt-1 line-clamp-2 text-[10px] text-rose-500" title={errored}>
          {errored}
        </p>
      ) : (
        <>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-[10px] text-gray-500">
            {step?.degraded && (
              <span
                title="This model never returned valid JSON — its answer is shown but it contributes no claims."
                className="rounded bg-amber-100 px-1 text-amber-700 dark:bg-amber-950 dark:text-amber-300"
              >
                ⚠ unstructured
              </span>
            )}
            {output?.claims?.length ? <span>{output.claims.length} claims</span> : null}
            {step && unsupportedFacts(step) > 0 && (
              <span
                title="Claims marked as facts with no stated basis"
                className="rounded bg-amber-100 px-1 text-amber-700 dark:bg-amber-950 dark:text-amber-300"
              >
                {unsupportedFacts(step)} unsupported
              </span>
            )}
            {step?.phase === "critique" && (
              <span>
                <span className="text-emerald-600">✓{accepted}</span>{" "}
                <span className="text-rose-500">✗{rejected}</span>
              </span>
            )}
            {typeof output?.confidence === "number" && (
              <span>conf {output.confidence.toFixed(2)}</span>
            )}
            {output?.position_changed && (
              <span
                title={output.change_trigger || "no trigger given"}
                className={output.change_trigger ? "text-gray-500" : "text-rose-500"}
              >
                {output.change_trigger ? "↻ changed" : "↻ changed (no reason)"}
              </span>
            )}
          </div>
          {step && (
            <button
              onClick={() => setOpen((o) => !o)}
              className="mt-1 self-start text-[10px] text-brand hover:underline"
            >
              {open ? "collapse ▴" : "expand ▾"}
            </button>
          )}
        </>
      )}

      {open && step && (
        <div className="mt-2 border-t border-gray-100 pt-2 dark:border-gray-800">
          {rejected > 0 && (
            <div className="mb-2">
              <div className="text-[10px] font-semibold uppercase tracking-wide text-rose-500">
                Rejected ({rejected})
              </div>
              <ul className="mt-0.5 space-y-1">
                {output?.rejected_claims?.map((r, i) => (
                  <li key={i} className="text-[11px] text-gray-600 dark:text-gray-300">
                    <span className="text-gray-400">{r.claim_id}</span> — {r.reason}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {accepted > 0 && (
            <details className="mb-2">
              <summary className="cursor-pointer text-[10px] font-semibold uppercase tracking-wide text-emerald-600">
                Accepted ({accepted})
              </summary>
              <ul className="mt-0.5 space-y-1">
                {output?.accepted_claims?.map((a, i) => (
                  <li key={i} className="text-[11px] text-gray-600 dark:text-gray-300">
                    <span className="text-gray-400">{a.claim_id}</span>
                    {a.note ? ` — ${a.note}` : ""}
                  </li>
                ))}
              </ul>
            </details>
          )}
          {previous && (
            <button
              onClick={() => setShowDiff((d) => !d)}
              className="mb-2 rounded border border-gray-300 px-1.5 py-0.5 text-[10px] text-gray-600 dark:border-gray-600 dark:text-gray-300"
            >
              {showDiff ? "show answer" : `⇄ diff vs round ${previous.round}`}
            </button>
          )}
          {showDiff && previous ? (
            <TextDiff
              before={stepAnswer(previous)}
              after={body}
              labelBefore={`R${previous.round}`}
              labelAfter={`R${step.round}`}
            />
          ) : (
            <MessageRenderer content={body} />
          )}
        </div>
      )}
    </div>
  );
}

function ConvergenceStrip({
  trace,
  decision,
}: {
  trace: ConvergenceTrace;
  decision?: { continue: boolean; reason: string };
}) {
  return (
    <div className="my-2 rounded-lg border border-indigo-200 bg-indigo-50/60 px-3 py-1.5 text-[11px] dark:border-indigo-900 dark:bg-indigo-950/40">
      <span className="font-semibold text-indigo-900 dark:text-indigo-200">
        {trace.approvals.length}/{trace.responded.length} approved
      </span>
      <span className="mx-2 text-indigo-400">·</span>
      <span className="text-indigo-800 dark:text-indigo-300">
        {trace.open_objection_count} open objection{trace.open_objection_count === 1 ? "" : "s"}
      </span>
      <span className="mx-2 text-indigo-400">·</span>
      <span
        className="text-indigo-800 dark:text-indigo-300"
        title="Lexical overlap between the panel's claims. Low overlap can mean genuine disagreement or simply different wording."
      >
        claim overlap {Math.round(trace.claim_overlap * 100)}%
      </span>
      {decision && (
        <div className="mt-0.5 text-indigo-700/80 dark:text-indigo-300/80">
          {decision.continue ? "▸ continue" : "■ stop"} — {decision.reason}
        </div>
      )}
    </div>
  );
}

/** Borda result for Quick mode — the cheap arm's answer, and how the panel ranked it. */
function VotePanel({
  vote,
  nameOf,
}: {
  vote: VoteResult;
  nameOf: (laneId: string) => string;
}) {
  const top = vote.ranking?.[0]?.score || 1;
  return (
    <section className="rounded-lg border border-gray-200 bg-white p-3 dark:border-gray-700 dark:bg-gray-900">
      <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-gray-500">
        Panel vote · Borda count · {vote.voters} ballot{vote.voters === 1 ? "" : "s"}
      </div>
      <div className="space-y-1">
        {(vote.ranking ?? []).map((entry, index) => (
          <div key={entry.lane_id} className="flex items-center gap-2 text-xs">
            <span className="w-4 text-gray-400">{index + 1}.</span>
            <span className="w-40 truncate text-gray-700 dark:text-gray-200">
              {nameOf(entry.lane_id)}
            </span>
            <div className="h-2 flex-1 rounded bg-gray-100 dark:bg-gray-800">
              <div
                className={`h-2 rounded ${index === 0 ? "bg-emerald-500" : "bg-indigo-400"}`}
                style={{ width: `${Math.max(4, (entry.score / top) * 100)}%` }}
              />
            </div>
            <span className="w-16 text-right text-gray-500">
              {entry.score} pts{entry.first_place_votes ? ` · ${entry.first_place_votes}×1st` : ""}
            </span>
          </div>
        ))}
      </div>
    </section>
  );
}

/**
 * A deliberation panel, rendered inline in the main content area alongside the chat
 * sidebar — clicking a deliberation should feel like opening a chat, not leaving the app.
 */
export function DeliberationView({ runId }: { runId: string }) {
  const navigate = useNavigate();
  const { live, run, refresh } = useDeliberation(runId);
  const [showAnalysis, setShowAnalysis] = useState(false);
  const [busy, setBusy] = useState("");

  const steps = useMemo(() => mergeSteps(run, live), [run, live]);
  const participants = run?.participants.filter((p) => p.role === "responder") ?? [];
  const traces = live.traces.length ? live.traces : (run?.convergence ?? []);
  const metrics = live.metrics ?? run?.metrics ?? null;
  const vote = (run?.vote && "ranking" in run.vote ? (run.vote as VoteResult) : null);
  const synthesis = live.synthesis?.answer
    ? live.synthesis
    : run?.synthesis
      ? {
          answer: run.synthesis,
          minority_report: run.minority_report,
          do_now: run.extraction?.do_now,
          consider_later: run.extraction?.consider_later,
          skip: run.extraction?.skip,
          critique: run.synthesis_critique,
          by: null,
        }
      : null;

  const maxRound = Math.max(
    0,
    ...steps.filter((s) => s.round < 90).map((s) => s.round),
    live.round >= 0 ? live.round : 0,
  );
  const rounds = Array.from({ length: maxRound + 1 }, (_, i) => i);
  const running = !live.finished && (live.started || run?.running);

  const stepFor = (round: number, laneId: string) =>
    steps.find((s) => s.round === round && s.lane_id === laneId);
  const liveFor = (round: number, laneId: string) =>
    Object.values(live.steps).find((s) => s.round === round && s.laneId === laneId);

  async function onStop() {
    setBusy("stop");
    await stopDeliberation(runId).catch(() => undefined);
    setBusy("");
  }

  async function onContinue() {
    setBusy("continue");
    try {
      const res = await continueInChat(runId);
      navigate(`/c/${res.session_id}`);
    } catch (e) {
      alert((e as Error).message);
      setBusy("");
    }
  }

  async function onExport() {
    setBusy("export");
    try {
      const res = await exportDeliberationPdf(runId);
      const a = document.createElement("a");
      a.href = mediaUrl(res.url);
      a.download = res.download_name;
      document.body.appendChild(a);
      a.click();
      a.remove();
    } catch (e) {
      alert((e as Error).message);
    }
    setBusy("");
  }

  return (
    <div className="flex min-h-0 flex-1">
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-2 border-b border-gray-200 px-3 py-2 dark:border-gray-700">
          <span className="truncate text-sm font-semibold text-gray-800 dark:text-gray-100">
            ⚖️ {run?.title || "Deliberation"}
          </span>
          <span className="ml-auto flex items-center gap-2">
            {running && (
              <button
                onClick={onStop}
                disabled={busy === "stop"}
                className="rounded border border-rose-300 px-2 py-1 text-xs text-rose-600 hover:bg-rose-50 dark:border-rose-800 dark:hover:bg-rose-950"
              >
                ⏹ Stop
              </button>
            )}
            <button
              onClick={() => setShowAnalysis((s) => !s)}
              className="rounded border border-gray-300 px-2 py-1 text-xs text-gray-600 hover:bg-gray-100 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
            >
              📊 Analysis
            </button>
            <button
              onClick={onExport}
              disabled={busy === "export"}
              title="Export the whole deliberation — rounds, objections, synthesis, dissent"
              className="rounded border border-gray-300 px-2 py-1 text-xs text-gray-600 hover:bg-gray-100 disabled:opacity-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
            >
              {busy === "export" ? "…" : "⬇ PDF"}
            </button>
          </span>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto p-3">
          {/* Full width on purpose: panelists sit side by side, so every extra pixel buys
              a wider column per model, and the synthesis usually carries a comparison table. */}
          <div className="space-y-3">
            <div className="rounded-lg border-l-4 border-brand bg-indigo-50 px-3 py-2 dark:bg-indigo-950/40">
              <div className="text-[10px] font-semibold uppercase tracking-wide text-brand">
                Question
              </div>
              <div className="whitespace-pre-wrap text-sm text-gray-800 dark:text-gray-100">
                {run?.prompt ?? ""}
              </div>
              {!!run?.images?.length && (
                <div className="mt-2 flex flex-wrap gap-2">
                  {run.images.map((img) => (
                    <a key={img.id} href={mediaUrl(img.url)} target="_blank" rel="noreferrer">
                      <img
                        src={mediaUrl(img.url)}
                        alt={img.filename}
                        title={img.filename}
                        className="h-20 rounded border border-gray-300 object-cover dark:border-gray-600"
                      />
                    </a>
                  ))}
                </div>
              )}
            </div>

            {rounds.map((round) => {
              const trace = traces.find((t) => t.round === round);
              const anyStep = participants.some(
                (p) => stepFor(round, p.lane_id) || liveFor(round, p.lane_id),
              );
              if (!anyStep && round > 0) return null;
              return (
                <section key={round}>
                  <div className="mb-1 flex items-center gap-2">
                    <span className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
                      {round === 0 ? "Round 0 · independent drafts" : `Round ${round} · peer review`}
                    </span>
                    {live.round === round && running && <Spinner />}
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {participants.map((p) => {
                      const step = stepFor(round, p.lane_id);
                      const ls = liveFor(round, p.lane_id);
                      return (
                        <StepCard
                          key={`${round}:${p.lane_id}`}
                          step={step}
                          model={p.model}
                          previous={round > 0 ? stepFor(round - 1, p.lane_id) : undefined}
                          running={!step && ls?.status === "running"}
                          chars={ls?.chars ?? 0}
                          errored={ls?.status === "error" ? ls.error : undefined}
                        />
                      );
                    })}
                  </div>
                  {trace && (
                    <ConvergenceStrip trace={trace} decision={live.decisions[round]} />
                  )}
                </section>
              );
            })}

            {vote && vote.ranking && vote.ranking.length > 0 && (
              <VotePanel
                vote={vote}
                nameOf={(laneId) =>
                  participants.find((p) => p.lane_id === laneId)?.model ?? laneId.slice(0, 6)
                }
              />
            )}

            {live.notice && (
              <div className="rounded border border-amber-300 bg-amber-50 px-3 py-1.5 text-xs text-amber-700 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-300">
                {live.notice}
              </div>
            )}
            {(live.error || run?.error) && (
              <div className="rounded border border-rose-300 bg-rose-50 px-3 py-1.5 text-xs text-rose-700 dark:border-rose-800 dark:bg-rose-950 dark:text-rose-300">
                {live.error || run?.error}
              </div>
            )}

            {synthesis && (
              <section className="rounded-xl border-2 border-brand/40 bg-white p-3 dark:bg-gray-900">
                <div className="mb-2 flex items-center gap-2">
                  <span className="text-[11px] font-semibold uppercase tracking-wide text-brand">
                    Synthesis
                  </span>
                  {synthesis.by && (
                    <span className="text-[10px] text-gray-400">
                      by {synthesis.by.model} ({synthesis.by.role})
                    </span>
                  )}
                  {run && (
                    <span className="ml-auto text-[10px] text-gray-400">
                      {run.converged ? "✓ converged" : "⚠ no consensus"} · {run.rounds_used} rounds ·{" "}
                      {run.total_calls} calls · {(run.wall_ms / 1000).toFixed(0)}s
                    </span>
                  )}
                </div>
                <div id="delib-synthesis">
                  <MessageRenderer content={synthesis.answer} />
                </div>

                {synthesis.minority_report && (
                  <div className="mt-3 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 dark:border-amber-800 dark:bg-amber-950/50">
                    <div className="text-[10px] font-semibold uppercase tracking-wide text-amber-700 dark:text-amber-300">
                      ⚠ Minority report
                    </div>
                    <div className="mt-1 text-xs text-amber-900 dark:text-amber-200">
                      <MessageRenderer content={synthesis.minority_report} />
                    </div>
                  </div>
                )}

                {!!(
                  (synthesis.do_now?.length ?? 0) ||
                  (synthesis.consider_later?.length ?? 0) ||
                  (synthesis.skip?.length ?? 0)
                ) && (
                  <div className="mt-3 grid gap-2 sm:grid-cols-3">
                    {(
                      [
                        ["Do now", synthesis.do_now, "border-emerald-300"],
                        ["Consider later", synthesis.consider_later, "border-sky-300"],
                        ["Skip", synthesis.skip, "border-gray-300"],
                      ] as const
                    ).map(([label, items, border]) => (
                      <div key={label} className={`rounded-lg border ${border} p-2 dark:border-gray-700`}>
                        <div className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">
                          {label}
                        </div>
                        <ul className="mt-1 space-y-0.5 text-[11px] text-gray-700 dark:text-gray-200">
                          {(items ?? []).map((it, i) => (
                            <li key={i}>• {it}</li>
                          ))}
                        </ul>
                      </div>
                    ))}
                  </div>
                )}

                <div className="mt-3 flex flex-wrap gap-2">
                  <button
                    onClick={() => navigator.clipboard.writeText(synthesis.answer)}
                    className="rounded border border-gray-300 px-2 py-1 text-xs text-gray-600 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
                  >
                    📋 Copy
                  </button>
                  <button
                    onClick={onContinue}
                    disabled={busy === "continue"}
                    className="rounded border border-gray-300 px-2 py-1 text-xs text-gray-600 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
                  >
                    ↗ Continue in chat
                  </button>
                  <button
                    onClick={() => void refresh()}
                    className="rounded border border-gray-300 px-2 py-1 text-xs text-gray-600 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
                  >
                    ⟳ Refresh
                  </button>
                </div>
              </section>
            )}
          </div>
        </div>
      </div>

      {showAnalysis && (
        <DeliberationAnalysis
          traces={traces}
          metrics={metrics}
          participants={participants}
          totalCalls={run?.total_calls ?? 0}
          wallMs={run?.wall_ms ?? 0}
          onClose={() => setShowAnalysis(false)}
        />
      )}
    </div>
  );
}
