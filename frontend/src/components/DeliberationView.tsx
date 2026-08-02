import { useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router";
import { MessageRenderer, CodeFoldContext } from "./MessageRenderer";
import { TextDiff } from "./TextDiff";
import { DeliberationAnalysis } from "./DeliberationAnalysis";
import { DownloadPdfButton } from "./LaneColumn";
import { FilesPanel } from "./FilesPanel";
import { PromptField } from "./ComposerExtras";
import { SnapshotsPanel } from "./SnapshotsPanel";
import type { Attachment } from "../api/types";
import {
  askFollowup,
  continueInChat,
  exportDeliberation,
  rerunDeliberation,
  stepAnswer,
  stopDeliberation,
  unsupportedFacts,
  type DeliberationFormat,
} from "../api/deliberation";
import type { ConvergenceTrace, DeliberationStep, VoteResult } from "../api/deliberation";
import { mergeSteps, useDeliberation } from "../hooks/useDeliberation";
import { apiFetch, mediaUrl } from "../api/client";
import { downloadMessagePdf } from "../utils/messagePdf";
import { useDismiss } from "../hooks/useDismiss";

/** Copy any block of text, with the usual "did it work?" feedback. */
function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      onClick={() => {
        navigator.clipboard.writeText(text);
        setDone(true);
        setTimeout(() => setDone(false), 1200);
      }}
      title="Copy to clipboard"
      className="rounded border border-gray-300 px-1.5 py-0.5 text-[10px] text-gray-600 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
    >
      {done ? "✓ Copied" : `📋 ${label}`}
    </button>
  );
}

/** A single answer blown up to fill the window — panel cards are necessarily narrow. */
function AnswerModal({
  title,
  content,
  onClose,
}: {
  title: string;
  content: string;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div
      onClick={onClose}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-6"
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="flex max-h-full w-full max-w-4xl flex-col rounded-xl bg-white shadow-xl dark:bg-gray-900"
      >
        <div className="flex items-center gap-2 border-b border-gray-200 px-4 py-2 dark:border-gray-700">
          <span className="truncate text-sm font-semibold text-gray-800 dark:text-gray-100">
            {title}
          </span>
          <span className="ml-auto flex items-center gap-2">
            <CopyButton text={content} />
            <button
              onClick={onClose}
              title="Close (Esc)"
              className="rounded px-1.5 text-sm text-gray-400 hover:text-rose-500"
            >
              ✕
            </button>
          </span>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          <MessageRenderer content={content} />
        </div>
      </div>
    </div>
  );
}

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
  sessionId,
  onPin,
  onExpand,
  onContinue,
  forceOpen,
  defaultOpen,
  openSignal,
  synthesisText,
}: {
  step?: DeliberationStep;
  model: string;
  previous?: DeliberationStep;
  running: boolean;
  chars: number;
  errored?: string;
  sessionId?: string;
  onPin?: (step: DeliberationStep) => Promise<void>;
  onExpand?: (title: string, content: string) => void;
  onContinue?: (step: DeliberationStep) => Promise<void>;
  /** Search opens every matching card, so a hit is never hidden behind "expand". */
  forceOpen?: boolean;
  /** Rounds worth reading straight away (the drafts and the final wording). */
  defaultOpen?: boolean;
  /** Bumped by the header's expand-all / collapse-all control. */
  openSignal?: { signal: number; open: boolean };
  /** The agreed answer, so a panelist can be diffed against what the panel settled on. */
  synthesisText?: string | null;
}) {
  const [open, setOpen] = useState(!!defaultOpen);
  const [diffMode, setDiffMode] = useState<"none" | "previous" | "synthesis">("none");
  const [pinned, setPinned] = useState(false);
  const [showRaw, setShowRaw] = useState(false);
  const bodyRef = useRef<HTMLDivElement>(null);
  const output = step?.output;
  const accepted = output?.accepted_claims?.length ?? 0;
  const rejected = output?.rejected_claims?.length ?? 0;
  const body = step ? stepAnswer(step) : "";
  const isOpen = open || !!forceOpen;

  // Apply a panel-wide expand/collapse command whenever one fires.
  useEffect(() => {
    if (openSignal && openSignal.signal > 0) setOpen(openSignal.open);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openSignal?.signal]);

  return (
    <div
      data-step-card
      className="flex min-w-0 flex-1 flex-col rounded-lg border border-gray-200 bg-white p-2 dark:border-gray-700 dark:bg-gray-900"
    >
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
        /* A provider failure is not the user's problem to decode — say what happened and
           keep the stack trace one click away. */
        <div className="mt-1">
          <p className="text-[10px] text-rose-500">
            This model didn't answer — it is left out of the approval count.
          </p>
          <button
            onClick={() => setShowRaw((v) => !v)}
            className="mt-0.5 text-[10px] text-gray-400 hover:underline"
          >
            {showRaw ? "hide details" : "details"}
          </button>
          {showRaw && (
            <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap break-all rounded bg-gray-100 p-1 text-[9px] text-gray-500 dark:bg-gray-800 dark:text-gray-400">
              {errored}
            </pre>
          )}
        </div>
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
              {isOpen ? "collapse ▴" : "expand ▾"}
            </button>
          )}
        </>
      )}

      {isOpen && step && (
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
          <div className="mb-2 flex flex-wrap gap-1">
            {previous && (
              <button
                onClick={() =>
                  setDiffMode((d) => (d === "previous" ? "none" : "previous"))
                }
                className="rounded border border-gray-300 px-1.5 py-0.5 text-[10px] text-gray-600 dark:border-gray-600 dark:text-gray-300"
              >
                {diffMode === "previous" ? "show answer" : `⇄ vs round ${previous.round}`}
              </button>
            )}
            {synthesisText && (
              <button
                onClick={() =>
                  setDiffMode((d) => (d === "synthesis" ? "none" : "synthesis"))
                }
                title="What the panel's agreed answer kept, dropped or reworded from this one"
                className="rounded border border-gray-300 px-1.5 py-0.5 text-[10px] text-gray-600 dark:border-gray-600 dark:text-gray-300"
              >
                {diffMode === "synthesis" ? "show answer" : "⇄ vs synthesis"}
              </button>
            )}
          </div>
          {diffMode === "previous" && previous ? (
            <TextDiff
              before={stepAnswer(previous)}
              after={body}
              labelBefore={`R${previous.round}`}
              labelAfter={`R${step.round}`}
            />
          ) : diffMode === "synthesis" && synthesisText ? (
            <TextDiff
              before={body}
              after={synthesisText}
              labelBefore={model}
              labelAfter="synthesis"
            />
          ) : (
            <div ref={bodyRef} className="min-w-0 overflow-x-auto">
              <MessageRenderer content={body} />
            </div>
          )}

          {/* Same actions a chat answer gets — this answer is a transcript message too. */}
          {!!body && (
            <div className="mt-2 flex flex-wrap items-center gap-1 border-t border-gray-100 pt-2 dark:border-gray-800">
              <CopyButton text={body} />
              {onExpand && (
                <button
                  onClick={() => onExpand(`${model} · round ${step.round}`, body)}
                  title="Open this answer full-screen"
                  className="rounded border border-gray-300 px-1.5 py-0.5 text-[10px] text-gray-600 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
                >
                  ⛶ Expand
                </button>
              )}
              {onPin && (
                <button
                  onClick={async () => {
                    await onPin(step);
                    setPinned(true);
                    setTimeout(() => setPinned(false), 1500);
                  }}
                  title="Pin this answer to compare across runs"
                  className="rounded border border-gray-300 px-1.5 py-0.5 text-[10px] text-gray-600 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
                >
                  {pinned ? "✓ Pinned" : "📌 Pin"}
                </button>
              )}
              {sessionId && step.message_id && (
                <DownloadPdfButton
                  onDownload={() =>
                    downloadMessagePdf(sessionId, step.message_id!, bodyRef.current)
                  }
                />
              )}
              {onContinue && (
                <button
                  onClick={() => void onContinue(step)}
                  title="Carry this answer into a normal chat with the model that wrote it"
                  className="rounded border border-gray-300 px-1.5 py-0.5 text-[10px] text-gray-600 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
                >
                  ↗ Continue
                </button>
              )}
            </div>
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
      <div className="mt-0.5 text-[10px] text-indigo-500/70 dark:text-indigo-300/50">
        Approval is the gate; claim overlap is only how much of the wording matches — two
        models can agree completely and still score low.
      </div>
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
  const [showFiles, setShowFiles] = useState(false);
  const [showPins, setShowPins] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [codeFold, setCodeFold] = useState({ signal: 0, collapsed: false });
  const [modal, setModal] = useState<{ title: string; content: string } | null>(null);
  const [query, setQuery] = useState("");
  const [compact, setCompact] = useState(
    () => localStorage.getItem("multichat_delib_density") === "compact",
  );
  const [followupText, setFollowupText] = useState("");
  const [questionOpen, setQuestionOpen] = useState(false);
  const [questionClipped, setQuestionClipped] = useState(false);
  const [toast, setToast] = useState<{ kind: "error" | "info"; text: string } | null>(null);
  // Round 0 and the final round open by default, so the toggle starts in the "open"
  // position — the first press should collapse, not re-open what is already open.
  const [cardsOpen, setCardsOpen] = useState({ signal: 0, open: true });
  const [elapsed, setElapsed] = useState(0);
  const questionRef = useRef<HTMLDivElement>(null);
  const followupRef = useRef<HTMLDivElement>(null);
  const [followupFiles, setFollowupFiles] = useState<Attachment[]>([]);
  const [busy, setBusy] = useState("");
  const exportRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);

  useDismiss(exportRef, exportOpen, () => setExportOpen(false));

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
  // Rough shape of the run, for the progress line: drafts + one review per round, plus
  // the synthesis pass. Deliberately an estimate — the gate can stop it early.
  const maxRounds = Number((run?.config as { max_rounds?: number })?.max_rounds ?? 0);
  const quick = (run?.config as { mode?: string })?.mode === "quick";
  const panelSize = Math.max(1, participants.length);
  const expectedCalls = quick
    ? panelSize * 2
    : panelSize * (1 + Math.max(1, maxRounds)) + 2;
  const doneSteps =
    steps.length + Object.values(live.steps).filter((s) => s.status === "running").length;
  const hasCode = useMemo(
    () =>
      steps.some((s) => stepAnswer(s).includes("```")) ||
      (synthesis?.answer ?? "").includes("```"),
    [steps, synthesis],
  );

  const stepFor = (round: number, laneId: string) =>
    steps.find((s) => s.round === round && s.lane_id === laneId);
  const liveFor = (round: number, laneId: string) =>
    Object.values(live.steps).find((s) => s.round === round && s.laneId === laneId);

  // Search inside the run. Cheap on purpose: a panel is a handful of answers, so a
  // lowercase substring scan beats pulling in an index, and it stays exact. The filter
  // runs off a DEFERRED copy of the query — matching re-mounts every hit's markdown, and
  // that must never be on the critical path of a keystroke.
  const deferredQuery = useDeferredValue(query);
  const needle = deferredQuery.trim().toLowerCase();
  const stepMatches = useCallback(
    (step?: DeliberationStep) =>
      !needle ||
      !!step &&
        ((step.model || "").toLowerCase().includes(needle) ||
          stepAnswer(step).toLowerCase().includes(needle)),
    [needle],
  );
  const matchCount = useMemo(
    () =>
      !needle
        ? 0
        : steps.filter((s) => stepMatches(s)).length +
          ((synthesis?.answer ?? "").toLowerCase().includes(needle) ? 1 : 0),
    [needle, steps, stepMatches, synthesis],
  );

  // Follow the panel while it works, but stop fighting the user the moment they scroll
  // up to read an earlier round. Mirrors the chat transcript's behaviour.
  const pinToBottom = useCallback(() => {
    const el = scrollRef.current;
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, []);

  useEffect(() => {
    if (running) pinToBottom();
  }, [steps.length, live.round, live.finished, running, pinToBottom]);

  // Mermaid renders ~300ms after the text lands and grows the page; without this the
  // view is left stranded mid-transcript.
  useEffect(() => {
    const el = contentRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => pinToBottom());
    ro.observe(el);
    return () => ro.disconnect();
  }, [pinToBottom]);

  // Escape stops a running panel — the same reflex as Escape in a chat.
  useEffect(() => {
    if (!running) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") void stopDeliberation(runId).catch(() => undefined);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [running, runId]);

  // Reading shortcuts. Skipped whenever the user is typing somewhere.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      if (el && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "/") {
        e.preventDefault();
        followupRef.current?.querySelector("textarea")?.focus();
      } else if (e.key === "e") {
        setCardsOpen((c) => ({ signal: c.signal + 1, open: !c.open }));
      } else if (e.key === "j" || e.key === "k") {
        // Panelists sit side by side, so the unit of navigation is the round, not the
        // card — stepping through cards in one row would look like nothing happened.
        const sections = Array.from(
          document.querySelectorAll<HTMLElement>("[data-round-section]"),
        );
        if (!sections.length) return;
        const tops = sections.map((s) => s.getBoundingClientRect().top);
        const at = tops.findIndex((t) => t > 90);
        const current = at === -1 ? sections.length - 1 : at;
        const next =
          e.key === "j"
            ? Math.min(current + 1, sections.length - 1)
            : Math.max(current - 1, 0);
        // Instant, not smooth: keyboard navigation should feel like a jump, and smooth
        // scrolling is animation-frame driven (so it stalls on a backgrounded tab).
        sections[next]?.scrollIntoView({ block: "start" });
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Commands raised from the command palette (which lives outside this component).
  useEffect(() => {
    const onCommand = (e: Event) => {
      const action = (e as CustomEvent<string>).detail;
      if (action === "analysis") setShowAnalysis((s) => !s);
      else if (action === "stop") void onStop();
      else if (action === "rerun") void onRerun();
      else if (action === "export") void onExport("pdf");
      else if (action === "expand") setCardsOpen((c) => ({ signal: c.signal + 1, open: true }));
    };
    window.addEventListener("multichat:deliberation", onCommand);
    return () => window.removeEventListener("multichat:deliberation", onCommand);
  });

  // Only offer "More" when the question genuinely doesn't fit — measured, not guessed at
  // from a character count, because wrapping depends on the window width.
  useEffect(() => {
    const el = questionRef.current;
    if (!el) return;
    const measure = () => {
      if (questionOpen) return; // clamp is off; the previous verdict still stands
      setQuestionClipped(el.scrollHeight > el.clientHeight + 1);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [run?.prompt, questionOpen]);

  // Errors belong next to the thing that failed, not in a modal that blocks the page.
  const fail = useCallback((e: unknown) => {
    setToast({ kind: "error", text: (e as Error).message || "Something went wrong" });
  }, []);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), toast.kind === "error" ? 6000 : 3000);
    return () => clearTimeout(t);
  }, [toast]);

  // A run takes anywhere from 15s to several minutes; show it moving.
  useEffect(() => {
    if (!running) return;
    setElapsed(0);
    const t = setInterval(() => setElapsed((s) => s + 1), 1000);
    return () => clearInterval(t);
  }, [running]);

  async function onStop() {
    setBusy("stop");
    await stopDeliberation(runId).catch(() => undefined);
    // The run winds up at the next safe point rather than dying mid-call, so say so
    // instead of leaving a dead-looking button.
    setToast({ kind: "info", text: "Stopping — the panel finishes the round it is in." });
  }

  async function onContinue() {
    setBusy("continue");
    try {
      const res = await continueInChat(runId);
      navigate(`/c/${res.session_id}`);
    } catch (e) {
      fail(e);
      setBusy("");
    }
  }

  /** Carry one panelist's answer into a chat with the model that wrote it. */
  async function continueFromStep(step: DeliberationStep) {
    try {
      const res = await continueInChat(runId, step.id);
      navigate(`/c/${res.session_id}`);
    } catch (e) {
      fail(e);
    }
  }

  async function onExport(fmt: DeliberationFormat) {
    setExportOpen(false);
    setBusy("export");
    try {
      const res = await exportDeliberation(runId, fmt);
      const a = document.createElement("a");
      a.href = mediaUrl(res.url);
      a.download = res.download_name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setToast({ kind: "info", text: `Saved ${res.download_name}` });
    } catch (e) {
      fail(e);
    }
    setBusy("");
  }

  /** Pin one answer so it can be compared against other runs later. */
  async function pinStep(step: DeliberationStep) {
    if (!run) return;
    const participant = participants.find((p) => p.lane_id === step.lane_id);
    await apiFetch("/api/snapshots", {
      method: "POST",
      body: JSON.stringify({
        session_id: run.session_id,
        prompt: run.prompt,
        model: step.model || participant?.model || "model",
        provider_name: participant?.provider_name || null,
        content: stepAnswer(step),
      }),
    }).catch(fail);
    setShowPins(true);
  }

  async function pinSynthesis() {
    if (!run || !synthesis?.answer) return;
    await apiFetch("/api/snapshots", {
      method: "POST",
      body: JSON.stringify({
        session_id: run.session_id,
        prompt: run.prompt,
        model: `${synthesis.by?.model ?? "panel"} (synthesis)`,
        provider_name: null,
        content: synthesis.answer,
      }),
    }).catch(fail);
    setShowPins(true);
  }

  async function onRerun() {
    setBusy("rerun");
    try {
      const res = await rerunDeliberation(runId);
      navigate(`/d/${res.run_id}`);
    } catch (e) {
      fail(e);
    }
    setBusy("");
  }

  async function onFollowup() {
    const prompt = followupText.trim();
    if (!prompt) return;
    setBusy("followup");
    try {
      const res = await askFollowup(
        runId,
        prompt,
        followupFiles.map((a) => a.id),
      );
      setFollowupText("");
      setFollowupFiles([]);
      navigate(`/d/${res.run_id}`);
    } catch (e) {
      fail(e);
    }
    setBusy("");
  }

  return (
    // min-w-0 matters: without it this flex item takes its content's max-content width, so
    // one unwrappable line (a degraded answer dumped as a single-line JSON blob) stretches
    // the entire page instead of scrolling inside its own card.
    <div className="flex min-h-0 min-w-0 flex-1">
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex min-w-0 flex-wrap items-center gap-2 border-b border-gray-200 px-3 py-2 dark:border-gray-700">
          <span className="truncate text-sm font-semibold text-gray-800 dark:text-gray-100">
            ⚖️ {run?.title || "Deliberation"}
          </span>
          {/* The verdict is the headline of the whole run; it doesn't belong in 10px grey. */}
          {run && !running && run.status !== "pending" && (
            <span
              title={`${run.rounds_used} round(s) · ${run.total_calls} model calls · ${(run.wall_ms / 1000).toFixed(0)}s`}
              className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold ${
                run.converged
                  ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300"
                  : run.status === "voted"
                    ? "bg-indigo-100 text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300"
                    : "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300"
              }`}
            >
              {run.converged
                ? "✓ converged"
                : run.status === "voted"
                  ? "🗳 voted"
                  : `⚠ ${run.status.replace("_", " ")}`}
            </span>
          )}
          <span className="ml-auto flex items-center gap-2">
            <span className="relative">
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="🔍 Find in this deliberation…"
                className="w-52 rounded border border-gray-300 px-2 py-1 text-xs focus:border-brand focus:outline-none dark:border-gray-600 dark:bg-gray-800"
              />
              {!!needle && (
                <span className="absolute right-2 top-1.5 text-[10px] text-gray-400">
                  {matchCount}
                </span>
              )}
            </span>
            <button
              onClick={() => {
                const next = !compact;
                setCompact(next);
                localStorage.setItem(
                  "multichat_delib_density",
                  next ? "compact" : "comfortable",
                );
              }}
              title={compact ? "Comfortable spacing" : "Compact spacing"}
              className="rounded border border-gray-300 px-2 py-1 text-xs text-gray-600 hover:bg-gray-100 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
            >
              {compact ? "▤" : "▥"}
            </button>
            {running && (
              <button
                onClick={onStop}
                disabled={busy === "stop"}
                title="Stop the panel (Esc) — it finishes the round it is in"
                className="rounded border border-rose-300 px-2 py-1 text-xs text-rose-600 hover:bg-rose-50 disabled:opacity-60 dark:border-rose-800 dark:hover:bg-rose-950"
              >
                {busy === "stop" ? "Stopping…" : "⏹ Stop"}
              </button>
            )}
            <button
              onClick={() => setCardsOpen((c) => ({ signal: c.signal + 1, open: !c.open }))}
              title={cardsOpen.open ? "Collapse every answer (e)" : "Expand every answer (e)"}
              className="rounded border border-gray-300 px-2 py-1 text-xs text-gray-600 hover:bg-gray-100 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
            >
              {cardsOpen.open ? "⤡" : "⤢"}
            </button>
            {hasCode && (
              <button
                onClick={() =>
                  setCodeFold((f) => ({ signal: f.signal + 1, collapsed: !f.collapsed }))
                }
                title={
                  codeFold.collapsed
                    ? "Expand every code block"
                    : "Collapse every code block"
                }
                className="rounded border border-gray-300 px-2 py-1 text-xs text-gray-600 hover:bg-gray-100 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
              >
                {codeFold.collapsed ? "▸</>" : "▾</>"}
              </button>
            )}
            <button
              onClick={() => setShowFiles((s) => !s)}
              title="Files generated in this deliberation"
              className="rounded border border-gray-300 px-2 py-1 text-xs text-gray-600 hover:bg-gray-100 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
            >
              📁 Files
            </button>
            <button
              onClick={() => setShowPins((s) => !s)}
              title="Pinned answers"
              className="rounded border border-gray-300 px-2 py-1 text-xs text-gray-600 hover:bg-gray-100 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
            >
              📌 Pins
            </button>
            <button
              onClick={() => setShowAnalysis((s) => !s)}
              className="rounded border border-gray-300 px-2 py-1 text-xs text-gray-600 hover:bg-gray-100 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
            >
              📊 Analysis
            </button>
            {!running && run && (
              <button
                onClick={onRerun}
                disabled={busy === "rerun"}
                title="Put the same question to the same panel again — useful when a panelist failed outright"
                className="rounded border border-gray-300 px-2 py-1 text-xs text-gray-600 hover:bg-gray-100 disabled:opacity-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
              >
                {busy === "rerun" ? "…" : "⟳ Re-run"}
              </button>
            )}
            <div className="relative" ref={exportRef}>
              <button
                onClick={() => setExportOpen((o) => !o)}
                disabled={busy === "export"}
                title="Export the whole deliberation — rounds, objections, synthesis, dissent"
                className="rounded border border-gray-300 px-2 py-1 text-xs text-gray-600 hover:bg-gray-100 disabled:opacity-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
              >
                {busy === "export" ? "…" : "⬇ Export ▾"}
              </button>
              {exportOpen && (
                <div className="absolute right-0 z-30 mt-1 w-56 rounded-lg border border-gray-200 bg-white py-1 text-xs shadow-lg dark:border-gray-700 dark:bg-gray-900">
                  {(
                    [
                      ["pdf", "PDF", "rounds, objections, synthesis"],
                      ["md", "Markdown", "the same, as text"],
                      ["docx", "Word (.docx)", "editable document"],
                      ["json", "JSON", "full audit trail — every step"],
                    ] as const
                  ).map(([fmt, label, hint]) => (
                    <button
                      key={fmt}
                      onClick={() => void onExport(fmt)}
                      className="block w-full px-3 py-1.5 text-left hover:bg-gray-100 dark:hover:bg-gray-800"
                    >
                      <span className="text-gray-800 dark:text-gray-100">{label}</span>
                      <span className="block text-[10px] text-gray-400">{hint}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          </span>
        </header>

        {/* Both panels are full-width bars (the chat page mounts them the same way), so
            they belong inside the column, under the header — not beside it. */}
        {showFiles && run && (
          <FilesPanel sessionId={run.session_id} onClose={() => setShowFiles(false)} />
        )}
        {showPins && <SnapshotsPanel onClose={() => setShowPins(false)} />}

        {/* Progress lives OUTSIDE the scroll area: a run takes minutes, and "is it still
            working?" is exactly the question you have while reading further down. */}
        {running && (
          <div className="border-b border-indigo-200 bg-indigo-50/70 px-3 py-1.5 text-[11px] text-indigo-800 dark:border-indigo-900 dark:bg-indigo-950/50 dark:text-indigo-200">
            <span className="font-semibold">
              {live.phase === "synthesis"
                ? "Synthesising…"
                : `Round ${Math.max(0, live.round)}${maxRounds ? ` of ${maxRounds}` : ""}`}
            </span>
            <span className="mx-2 text-indigo-400">·</span>
            {doneSteps} of ~{expectedCalls} model calls
            <span className="mx-2 text-indigo-400">·</span>
            {Math.floor(elapsed / 60)}m {String(elapsed % 60).padStart(2, "0")}s
            <div className="mt-1 h-1 w-full overflow-hidden rounded bg-indigo-100 dark:bg-indigo-900">
              <div
                className="h-1 rounded bg-brand transition-all"
                style={{
                  width: `${Math.min(100, Math.round((doneSteps / Math.max(1, expectedCalls)) * 100))}%`,
                }}
              />
            </div>
          </div>
        )}

        <div
          ref={scrollRef}
          onScroll={(e) => {
            const el = e.currentTarget;
            stickToBottom.current =
              el.scrollHeight - el.scrollTop - el.clientHeight < 60;
          }}
          className="min-h-0 flex-1 overflow-y-auto p-3"
        >
          {/* Full width on purpose: panelists sit side by side, so every extra pixel buys
              a wider column per model, and the synthesis usually carries a comparison table. */}
          <CodeFoldContext.Provider value={codeFold}>
          <div
            className={`${compact ? "space-y-1.5 text-[13px]" : "space-y-3"}`}
            ref={contentRef}
          >
            {(run?.thread?.length ?? 0) > 1 && (
              <div className="flex flex-wrap items-center gap-1 text-[11px]">
                <span className="text-gray-400">Thread:</span>
                {run!.thread!.map((t, i) => (
                  <button
                    key={t.id}
                    onClick={() => navigate(`/d/${t.id}`)}
                    title={t.prompt}
                    className={`max-w-[16rem] truncate rounded-full px-2 py-0.5 ${
                      t.id === run!.id
                        ? "bg-brand/10 font-semibold text-brand"
                        : "bg-gray-100 text-gray-600 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-300"
                    }`}
                  >
                    {i + 1}. {t.prompt}
                  </button>
                ))}
              </div>
            )}

            <div className="sticky top-0 z-10 rounded-lg border-l-4 border-brand bg-indigo-50 px-3 py-2 shadow-sm backdrop-blur-sm dark:bg-indigo-950/90">
              <div className="text-[10px] font-semibold uppercase tracking-wide text-brand">
                Question
              </div>
              {/* A long question is pinned to the top of the page, so it must not eat the
                  screen the panel's answers need. Clamped to three lines until asked. */}
              <div
                ref={questionRef}
                className={`whitespace-pre-wrap text-sm text-gray-800 dark:text-gray-100 ${
                  questionOpen ? "" : "line-clamp-3"
                }`}
              >
                {run?.prompt ?? ""}
              </div>
              {(questionClipped || questionOpen) && (
                <button
                  onClick={() => setQuestionOpen((o) => !o)}
                  className="mt-0.5 text-[11px] font-medium text-brand hover:underline"
                >
                  {questionOpen ? "Show less ▴" : "More ▾"}
                </button>
              )}
              {!!run?.documents?.length && (
                <div className="mt-1 flex flex-wrap gap-1">
                  {run.documents.map((d) => (
                    <span
                      key={d.id}
                      title={`${d.chars.toLocaleString()} characters given to every panelist`}
                      className="rounded bg-white/70 px-1.5 py-0.5 text-[10px] text-gray-600 dark:bg-gray-800 dark:text-gray-300"
                    >
                      📄 {d.filename}
                    </span>
                  ))}
                </div>
              )}
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
              const shown = participants.filter(
                (p) => !needle || stepMatches(stepFor(round, p.lane_id)),
              );
              if (needle && shown.length === 0) return null;
              return (
                <section key={round} data-round-section>
                  <div className="mb-1 flex items-center gap-2">
                    <span className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
                      {round === 0 ? "Round 0 · independent drafts" : `Round ${round} · peer review`}
                    </span>
                    {live.round === round && running && <Spinner />}
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {shown.map((p) => {
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
                          sessionId={run?.session_id}
                          onPin={pinStep}
                          onExpand={(title, content) => setModal({ title, content })}
                          onContinue={continueFromStep}
                          forceOpen={!!needle}
                          defaultOpen={round === 0 || round === maxRound}
                          openSignal={cardsOpen}
                          synthesisText={synthesis?.answer ?? null}
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
                <div id="delib-synthesis" className="min-w-0 overflow-x-auto">
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
                    onClick={() =>
                      setModal({ title: "Synthesis", content: synthesis.answer })
                    }
                    className="rounded border border-gray-300 px-2 py-1 text-xs text-gray-600 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
                  >
                    ⛶ Expand
                  </button>
                  <button
                    onClick={() => void pinSynthesis()}
                    title="Pin the synthesis to compare across runs"
                    className="rounded border border-gray-300 px-2 py-1 text-xs text-gray-600 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
                  >
                    📌 Pin
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
          </CodeFoldContext.Provider>
        </div>

        {/* Follow-ups go to the SAME panel: a new run, because the protocol has to start
            over for the convergence numbers to mean anything, but with the panel, the
            settings and the answer they already agreed on carried across. */}
        {run && !running && (
          <div className="border-t border-gray-200 p-3 dark:border-gray-700" ref={followupRef}>
            <PromptField
              value={followupText}
              onChange={setFollowupText}
              onSubmit={() => void onFollowup()}
              attachments={followupFiles}
              onAttachmentsChange={setFollowupFiles}
              placeholder="Ask this panel a follow-up… (paste or drop images and documents)"
              trailing={
                <button
                  onClick={() => void onFollowup()}
                  disabled={busy === "followup" || !followupText.trim()}
                  title="Put a follow-up question to the same panel, with what they already agreed as context"
                  className="rounded-lg bg-brand px-3 py-2 text-sm font-medium text-white disabled:opacity-40"
                >
                  {busy === "followup" ? "Starting…" : "Ask the panel"}
                </button>
              }
              footer={
                <span className="text-[10px] text-gray-400">
                  Runs the protocol again with the same panel — the answer above is given to
                  every panelist as context
                  {followupFiles.length > 0 &&
                    `, along with ${followupFiles.length} attachment${
                      followupFiles.length === 1 ? "" : "s"
                    }`}
                  .
                </span>
              }
            />
          </div>
        )}
      </div>

      {modal && (
        <AnswerModal
          title={modal.title}
          content={modal.content}
          onClose={() => setModal(null)}
        />
      )}

      {toast && (
        <div
          onClick={() => setToast(null)}
          className={`fixed bottom-4 right-4 z-50 max-w-sm cursor-pointer rounded-lg px-3 py-2 text-xs shadow-lg ${
            toast.kind === "error"
              ? "border border-rose-300 bg-rose-50 text-rose-800 dark:border-rose-800 dark:bg-rose-950 dark:text-rose-200"
              : "border border-gray-300 bg-white text-gray-700 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-200"
          }`}
        >
          {toast.kind === "error" ? "⚠ " : ""}
          {toast.text}
        </div>
      )}

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
