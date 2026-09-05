import { useCallback, useEffect, useRef, useState } from "react";
import { streamSSE } from "../api/client";
import {
  getDeliberation,
  type ConvergenceTrace,
  type DeliberationRun,
  type DeliberationStep,
  type PanelMetrics,
  type StepOutput,
} from "../api/deliberation";

export interface LiveStep {
  stepId: string;
  round: number;
  phase: string;
  laneId: string | null;
  /** Transcript message this answer was mirrored into (per-answer PDF needs it). */
  messageId?: string | null;
  model?: string;
  status: "running" | "done" | "error";
  chars: number;
  verdict?: string | null;
  degraded?: boolean;
  latencyMs?: number;
  output?: StepOutput;
  error?: string;
}

export interface LiveSynthesis {
  answer: string;
  minority_report?: string | null;
  do_now?: string[];
  consider_later?: string[];
  skip?: string[];
  critique?: { faithful?: boolean; issues?: { severity?: string; text?: string }[] } | null;
  by?: { model: string; role: string } | null;
}

export interface DeliberationLive {
  started: boolean;
  finished: boolean;
  round: number;
  phase: string;
  steps: Record<string, LiveStep>;
  traces: ConvergenceTrace[];
  decisions: Record<number, { continue: boolean; reason: string }>;
  metrics: PanelMetrics | null;
  synthesis: LiveSynthesis | null;
  status?: string;
  notice?: string;
  error?: string;
}

const EMPTY: DeliberationLive = {
  started: false,
  finished: false,
  round: -1,
  phase: "",
  steps: {},
  traces: [],
  decisions: {},
  metrics: null,
  synthesis: null,
};

/**
 * Drive one deliberation over SSE.
 *
 * The backend runs the panel in a detached task, so this hook can attach, detach and
 * re-attach freely: reconnecting replays every frame that was published while the page
 * was away. `run` is the persisted record, refetched whenever the stream settles.
 */
export function useDeliberation(runId: string | null) {
  const [live, setLive] = useState<DeliberationLive>(EMPTY);
  const [run, setRun] = useState<DeliberationRun | null>(null);
  const [loading, setLoading] = useState(false);
  const [stateRunId, setStateRunId] = useState(runId);
  const controllerRef = useRef<AbortController | null>(null);
  const activeRunRef = useRef<string | null>(null);
  const generationRef = useRef(0);
  const refreshIdRef = useRef(0);

  const refresh = useCallback(async () => {
    if (!runId || activeRunRef.current !== runId) return;
    const generation = generationRef.current;
    const requestId = ++refreshIdRef.current;
    try {
      const persisted = await getDeliberation(runId);
      if (
        activeRunRef.current === runId &&
        generationRef.current === generation &&
        refreshIdRef.current === requestId
      ) {
        setRun(persisted);
      }
    } catch {
      /* the run may have been deleted; the page handles a null run */
    }
  }, [runId]);

  const handle = useCallback((event: string, data: any, isCurrent: () => boolean) => {
    setLive((prev) => {
      if (!isCurrent()) return prev;
      const next = { ...prev, steps: { ...prev.steps } };
      switch (event) {
        case "delib_start":
          next.started = true;
          break;
        case "round_start":
          next.round = data.round;
          next.phase = data.phase;
          break;
        case "step_start":
          next.steps[data.step_id] = {
            stepId: data.step_id,
            round: data.round,
            phase: data.phase,
            laneId: data.lane_id ?? null,
            model: data.model,
            status: "running",
            chars: 0,
          };
          break;
        case "step_progress": {
          const existing = next.steps[data.step_id];
          if (existing) next.steps[data.step_id] = { ...existing, chars: data.chars };
          break;
        }
        case "step_done": {
          const existing = next.steps[data.step_id];
          next.steps[data.step_id] = {
            ...(existing ?? {
              stepId: data.step_id,
              round: data.round,
              phase: data.phase,
              laneId: data.lane_id ?? null,
              chars: 0,
            }),
            model: data.model,
            status: "done",
            messageId: data.message_id ?? null,
            verdict: data.verdict,
            degraded: data.degraded,
            latencyMs: data.latency_ms,
            output: data.output,
          };
          break;
        }
        case "step_error": {
          const existing = next.steps[data.step_id];
          next.steps[data.step_id] = {
            ...(existing ?? {
              stepId: data.step_id,
              round: data.round,
              phase: data.phase,
              laneId: data.lane_id ?? null,
              chars: 0,
            }),
            status: "error",
            error: data.detail,
          };
          break;
        }
        case "convergence":
          next.traces = [...prev.traces.filter((t) => t.round !== data.round), data].sort(
            (a, b) => a.round - b.round,
          );
          break;
        case "round_decision":
          next.decisions = {
            ...prev.decisions,
            [data.round]: { continue: data.continue, reason: data.reason },
          };
          break;
        case "metrics":
          next.metrics = data;
          break;
        case "synthesis_start":
          next.synthesis = { answer: "", by: { model: data.model, role: data.role } };
          break;
        case "synthesis_done":
          next.synthesis = { ...(prev.synthesis ?? {}), ...data };
          break;
        case "delib_notice":
          next.notice = data.detail;
          break;
        case "delib_error":
          next.error = data.detail;
          break;
        case "delib_done":
          next.finished = true;
          next.status = data.status;
          break;
        default:
          break;
      }
      return next;
    });
  }, []);

  /** Open (or re-open) the stream. Safe to call on every mount. */
  const attach = useCallback(() => {
    if (!runId || activeRunRef.current !== runId) return;
    generationRef.current += 1;
    const generation = generationRef.current;
    controllerRef.current?.abort();
    setLive({ ...EMPTY, steps: {} });
    setLoading(true);
    const isCurrent = () =>
      activeRunRef.current === runId &&
      generationRef.current === generation &&
      !controller.signal.aborted;
    const controller = streamSSE(
      `/api/deliberations/${runId}/stream`,
      {},
      (evt) => {
        if (!isCurrent()) return;
        handle(evt.event, evt.data, isCurrent);
        // Pull canonical data when the panel finishes, even before the connection closes.
        if (evt.event === "delib_done") void refresh();
      },
      () => {
        if (!isCurrent()) return;
        setLoading(false);
        void refresh();
      },
      (err) => {
        if (!isCurrent()) return;
        setLoading(false);
        setLive((p) => isCurrent() ? { ...p, error: err.message } : p);
        void refresh();
      },
    );
    controllerRef.current = controller;
    // Fetch after allocating the generation so attach cannot invalidate its own refresh.
    void refresh();
  }, [runId, handle, refresh]);

  useEffect(() => {
    activeRunRef.current = runId;
    setStateRunId(runId);
    setRun(null);
    setLive(EMPTY);
    setLoading(false);
    if (runId) attach();
    return () => {
      activeRunRef.current = null;
      generationRef.current += 1;
      controllerRef.current?.abort();
      controllerRef.current = null;
    };
    // attach depends only on runId and stable callbacks, not on incoming live state.
  }, [runId, attach]);

  // Never expose a previous run during the render before the new identity's effect runs.
  const isCurrentRun = stateRunId === runId;
  return {
    live: isCurrentRun ? live : EMPTY,
    run: isCurrentRun ? run : null,
    loading: isCurrentRun ? loading : false,
    refresh,
    attach,
  };
}

/** Merge persisted steps with in-flight ones so a rejoined run renders completely. */
export function mergeSteps(
  run: DeliberationRun | null,
  live: DeliberationLive,
): DeliberationStep[] {
  const byId = new Map<string, DeliberationStep>();
  for (const step of run?.steps ?? []) byId.set(step.id, step);
  for (const step of Object.values(live.steps)) {
    if (step.status !== "done" || !step.output) continue;
    byId.set(step.stepId, {
      id: step.stepId,
      lane_id: step.laneId,
      message_id: step.messageId ?? byId.get(step.stepId)?.message_id ?? null,
      round: step.round,
      phase: step.phase,
      label: null,
      model: step.model ?? null,
      verdict: step.verdict ?? null,
      output: step.output,
      degraded: !!step.degraded,
      error: null,
      latency_ms: step.latencyMs ?? null,
      usage: null,
    });
  }
  return [...byId.values()].sort((a, b) => a.round - b.round);
}
