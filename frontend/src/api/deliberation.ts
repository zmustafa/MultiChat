import { apiFetch } from "./client";

export interface PanelMember {
  provider_id: string;
  model: string;
}

export interface DeliberationParticipant extends PanelMember {
  lane_id: string;
  provider_name: string;
  role: string;
}

/** One model's structured contribution to a round. */
export interface StepOutput {
  answer?: string;
  revised_answer?: string;
  reasoning_summary?: string;
  assumptions?: string[];
  claims?: { id?: string; text?: string; kind?: string; support?: string | null }[];
  confidence?: number | null;
  verdict?: string;
  accepted_claims?: { peer?: string; claim_id?: string; note?: string }[];
  rejected_claims?: { peer?: string; claim_id?: string; reason?: string }[];
  no_material_disagreement?: boolean;
  position_changed?: boolean;
  change_trigger?: string | null;
}

export interface DeliberationStep {
  id: string;
  lane_id: string | null;
  round: number;
  phase: string;
  label: string | null;
  model: string | null;
  verdict: string | null;
  output: StepOutput;
  degraded: boolean;
  error: string | null;
  latency_ms: number | null;
  usage: { prompt_tokens?: number; completion_tokens?: number } | null;
}

/** Per-round convergence trace — why the panel did or didn't stop. */
export interface ConvergenceTrace {
  round: number;
  /** Share of responding peers that returned APPROVE — the gate, and what the UI shows. */
  agreement: number;
  /** Lexical overlap between claim sets. Relative signal only; cannot see paraphrase. */
  claim_overlap: number;
  diversity: number;
  aligned: boolean;
  labels: string[];
  matrix: number[][];
  self_deltas: Record<string, number>;
  stable: boolean;
  verdicts: Record<string, string | null>;
  responded: string[];
  approvals: string[];
  open_objections: {
    by: string;
    peer?: string;
    claim_id?: string;
    reason?: string;
  }[];
  open_objection_count: number;
  converged: boolean;
}

export interface PanelMetrics {
  influence?: Record<string, number>;
  influence_counts?: Record<string, number>;
  capitulation?: Record<string, number>;
  final_agreement?: number | null;
  final_overlap?: number | null;
  final_diversity?: number | null;
}

export interface VoteResult {
  ranking: { lane_id: string; label: string; score: number; first_place_votes: number }[];
  winner_lane_id: string | null;
  ballots: Record<string, string[]>;
  voters: number;
}

export interface DeliberationRun {
  id: string;
  session_id: string;
  turn_id: string;
  title: string;
  status: string;
  running: boolean;
  prompt: string;
  images: { id: string; filename: string; url: string }[];
  rounds_used: number;
  converged: boolean;
  config: Record<string, unknown>;
  convergence: ConvergenceTrace[];
  vote: VoteResult | Record<string, never>;
  metrics: PanelMetrics;
  synthesis: string | null;
  minority_report: string | null;
  extraction: { do_now?: string[]; consider_later?: string[]; skip?: string[] };
  synthesis_critique: { faithful?: boolean; issues?: { severity?: string; text?: string }[] };
  total_calls: number;
  wall_ms: number;
  error: string | null;
  created_at: string;
  participants: DeliberationParticipant[];
  steps: DeliberationStep[];
}

export interface CreateDeliberationBody {
  prompt: string;
  title?: string;
  participants: PanelMember[];
  judge?: PanelMember | null;
  max_rounds: number;
  synthesis: boolean;
  minority_report: boolean;
  critique_synthesis: boolean;
  /** "council" = full peer review; "quick" = draft + Borda vote (the cheap baseline). */
  mode?: "council" | "quick";
  evidence?: boolean;
  /** Images uploaded via /api/uploads, shown to the panel alongside the question. */
  attachment_ids?: string[];
}

export function createDeliberation(body: CreateDeliberationBody) {
  return apiFetch<{ run_id: string; session_id: string; turn_id: string }>(
    "/api/deliberations",
    { method: "POST", body: JSON.stringify(body) },
  );
}

export function getDeliberation(runId: string) {
  return apiFetch<DeliberationRun>(`/api/deliberations/${runId}`);
}

export function stopDeliberation(runId: string) {
  return apiFetch<{ stopping: boolean }>(`/api/deliberations/${runId}/stop`, {
    method: "POST",
  });
}

export function continueInChat(runId: string) {
  return apiFetch<{ session_id: string }>(`/api/deliberations/${runId}/continue`, {
    method: "POST",
  });
}

export function listDeliberations(limit = 25) {
  return apiFetch<
    {
      id: string;
      session_id: string;
      prompt: string;
      status: string;
      converged: boolean;
      rounds_used: number;
      total_calls: number;
      wall_ms: number;
      created_at: string;
    }[]
  >(`/api/deliberations?limit=${limit}`);
}

export interface Leaderboard {
  runs: number;
  models: {
    model: string;
    influence: number | null;
    capitulation: number | null;
    appearances: number;
  }[];
}

export function getLeaderboard() {
  return apiFetch<Leaderboard>("/api/deliberations/leaderboard");
}

export function classifyPrompt(prompt: string) {
  return apiFetch<{ complexity: string; recommend: string; reason: string }>(
    "/api/deliberations/classify",
    { method: "POST", body: JSON.stringify({ prompt }) },
  );
}

/** The answer body a step settled on, whichever phase produced it. */
export function stepAnswer(step: DeliberationStep): string {
  return (step.output.revised_answer || step.output.answer || "").trim();
}

/** Factual claims asserted with no stated basis — only meaningful in evidence mode. */
export function unsupportedFacts(step: DeliberationStep): number {
  return (step.output.claims ?? []).filter(
    (c) => (c.kind || "").toLowerCase() === "fact" && !(c.support || "").trim(),
  ).length;
}

export function exportDeliberationPdf(runId: string) {
  return apiFetch<{ url: string; download_name: string }>(
    `/api/deliberations/${runId}/export`,
    { method: "POST" },
  );
}

// ---------------------------------------------------------------------------
// Benchmark — does deliberation beat the cheap alternatives?
// ---------------------------------------------------------------------------

export const BENCH_ARMS = ["single", "vote", "synthesize", "council"] as const;
export type BenchArm = (typeof BENCH_ARMS)[number];

export const ARM_LABELS: Record<BenchArm, string> = {
  single: "Single model",
  vote: "Majority vote",
  synthesize: "Synthesize only",
  council: "Full deliberation",
};

export interface BenchSummary {
  avg_scores: Record<string, number | null>;
  wins: Record<string, number>;
  prompts: number;
  best_baseline: number | null;
  council: number | null;
  verdict: string;
}

export interface BenchRow {
  prompt: string;
  scores: Record<string, number>;
  reasons?: Record<string, string>;
  answers?: Record<string, string>;
  calls: number;
  council_rounds?: number;
  council_converged?: boolean;
  error?: string;
}
