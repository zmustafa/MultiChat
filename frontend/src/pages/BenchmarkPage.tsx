import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router";
import { streamSSE } from "../api/client";
import {
  ARM_LABELS,
  BENCH_ARMS,
  type BenchArm,
  type BenchRow,
  type BenchSummary,
} from "../api/deliberation";
import { useProviders } from "../hooks/useProviders";
import { ThemeToggle } from "../components/ThemeToggle";
import { MessageRenderer } from "../components/MessageRenderer";

const DEFAULT_PROMPTS = [
  "Should a 5-person startup run its production API on Kubernetes or a managed container service?",
  "Our checkout p99 latency regressed from 180ms to 900ms after a deploy. How should we triage it?",
];

interface Option {
  key: string;
  providerId: string;
  providerName: string;
  model: string;
}

function ArmBar({ arm, score, best }: { arm: string; score: number | null; best: boolean }) {
  return (
    <div className="flex items-center gap-2">
      <span className="w-36 truncate text-xs text-gray-600 dark:text-gray-300">
        {ARM_LABELS[arm as BenchArm] ?? arm}
      </span>
      <div className="h-3 flex-1 rounded bg-gray-100 dark:bg-gray-800">
        <div
          className={`h-3 rounded ${best ? "bg-emerald-500" : "bg-indigo-400"}`}
          style={{ width: `${((score ?? 0) / 10) * 100}%` }}
        />
      </div>
      <span className="w-10 text-right text-xs font-medium text-gray-700 dark:text-gray-200">
        {score == null ? "—" : score.toFixed(2)}
      </span>
    </div>
  );
}

/**
 * The decision gate: does deliberation actually beat the cheap alternatives?
 *
 * All four arms start from the same drafts, so the comparison isolates what happens *after*
 * the first answer rather than which model got lucky. A judge that is not on the panel then
 * scores them blind in one call.
 */
export function BenchmarkPage() {
  const navigate = useNavigate();
  const { data: providers = [] } = useProviders();
  const options = useMemo<Option[]>(() => {
    const out: Option[] = [];
    for (const p of providers) {
      const models = p.models?.length ? p.models : p.default_model ? [p.default_model] : [];
      for (const m of models) {
        out.push({ key: `${p.id}::${m}`, providerId: p.id, providerName: p.name, model: m });
      }
    }
    return out;
  }, [providers]);

  const [selected, setSelected] = useState<string[]>([]);
  const [judgeKey, setJudgeKey] = useState("");
  const [rounds, setRounds] = useState(2);
  const [arms, setArms] = useState<BenchArm[]>([...BENCH_ARMS]);
  const [prompts, setPrompts] = useState(DEFAULT_PROMPTS.join("\n"));
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState<string[]>([]);
  const [rows, setRows] = useState<BenchRow[]>([]);
  const [summary, setSummary] = useState<BenchSummary | null>(null);
  const [error, setError] = useState("");
  const [openRow, setOpenRow] = useState<number | null>(null);
  const ctrlRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (selected.length || !options.length) return;
    const picked: string[] = [];
    const seen = new Set<string>();
    for (const o of options) {
      if (picked.length >= 3) break;
      if (seen.has(o.providerId)) continue;
      seen.add(o.providerId);
      picked.push(o.key);
    }
    for (const o of options) {
      if (picked.length >= 3) break;
      if (!picked.includes(o.key)) picked.push(o.key);
    }
    setSelected(picked);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [options]);

  useEffect(() => () => ctrlRef.current?.abort(), []);

  const chosen = options.filter((o) => selected.includes(o.key));
  const judge = options.find((o) => o.key === judgeKey) || chosen[0] || null;
  const promptList = prompts.split("\n").map((p) => p.trim()).filter(Boolean);
  const perPrompt =
    chosen.length +
    (arms.includes("vote") ? chosen.length : 0) +
    (arms.includes("synthesize") ? 1 : 0) +
    (arms.includes("council") ? chosen.length * rounds + 1 : 0) +
    1;
  const totalCalls = perPrompt * promptList.length;

  function start() {
    setError("");
    if (chosen.length < 2) return setError("Pick at least two models.");
    if (!promptList.length) return setError("Add at least one prompt.");
    setRunning(true);
    setRows([]);
    setSummary(null);
    setProgress([]);
    ctrlRef.current?.abort();
    ctrlRef.current = streamSSE(
      "/api/deliberations/benchmark",
      {
        prompts: promptList,
        participants: chosen.map((c) => ({ provider_id: c.providerId, model: c.model })),
        judge: judge ? { provider_id: judge.providerId, model: judge.model } : null,
        max_rounds: rounds,
        arms,
      },
      (evt) => {
        const d = evt.data;
        if (evt.event === "prompt_start")
          setProgress((p) => [...p, `▸ prompt ${d.index + 1}: ${d.prompt.slice(0, 70)}…`]);
        else if (evt.event === "arm_done")
          setProgress((p) => [...p, `   ✓ ${ARM_LABELS[d.arm as BenchArm] ?? d.arm}`]);
        else if (evt.event === "scoring") setProgress((p) => [...p, "   ⚖ judging…"]);
        else if (evt.event === "prompt_done")
          setProgress((p) => [
            ...p,
            `   scores: ${Object.entries(d.scores ?? {})
              .map(([a, s]) => `${a}=${s}`)
              .join(" ")}`,
          ]);
        else if (evt.event === "bench_done") {
          setSummary(d.summary);
          setRows(d.results ?? []);
          setProgress((p) => [...p, `done — ${d.total_calls} calls in ${Math.round(d.wall_ms / 1000)}s`]);
        }
      },
      () => setRunning(false),
      (e) => {
        setError(e.message);
        setRunning(false);
      },
    );
  }

  const bestArm =
    summary &&
    Object.entries(summary.avg_scores)
      .filter(([, v]) => v != null)
      .sort((a, b) => (b[1] as number) - (a[1] as number))[0]?.[0];

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-2 border-b border-gray-200 px-3 py-2 dark:border-gray-700">
        <button
          onClick={() => navigate("/analytics")}
          className="rounded px-2 py-1 text-xs text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800"
        >
          ← Insights
        </button>
        <span className="text-sm font-semibold text-gray-800 dark:text-gray-100">
          Is deliberation worth it?
        </span>
        <span className="ml-auto">
          <ThemeToggle />
        </span>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        <div className="mx-auto max-w-4xl space-y-4">
          <p className="text-xs text-gray-500 dark:text-gray-400">
            Multi-model debate is expensive, and most of the measured benefit of a panel
            comes from plain voting rather than the discussion afterwards. This runs all four
            approaches from the <strong>same drafts</strong> and has an off-panel judge score
            them blind, so you can see whether the extra calls buy anything on your own
            questions.
          </p>

          <section className="grid gap-4 sm:grid-cols-2">
            <div>
              <label className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
                Panel ({chosen.length})
              </label>
              <div className="mt-1 max-h-36 overflow-y-auto rounded border border-gray-200 p-1 dark:border-gray-700">
                {options.map((o) => (
                  <label
                    key={o.key}
                    className="flex cursor-pointer items-center gap-2 rounded px-2 py-0.5 text-xs hover:bg-gray-50 dark:hover:bg-gray-800"
                  >
                    <input
                      type="checkbox"
                      checked={selected.includes(o.key)}
                      onChange={() =>
                        setSelected((prev) =>
                          prev.includes(o.key)
                            ? prev.filter((k) => k !== o.key)
                            : [...prev, o.key],
                        )
                      }
                    />
                    <span className="text-gray-800 dark:text-gray-100">{o.model}</span>
                    <span className="text-gray-400">{o.providerName}</span>
                  </label>
                ))}
              </div>
            </div>
            <div className="space-y-2">
              <div>
                <label className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
                  Judge (scores blind)
                </label>
                <select
                  value={judgeKey}
                  onChange={(e) => setJudgeKey(e.target.value)}
                  className="mt-1 w-full rounded border border-gray-300 bg-white px-2 py-1 text-xs dark:border-gray-600 dark:bg-gray-800"
                >
                  <option value="">Auto — first panel model</option>
                  {options.map((o) => (
                    <option key={o.key} value={o.key}>
                      {o.model} · {o.providerName}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
                  Arms
                </label>
                <div className="mt-1 flex flex-wrap gap-2 text-xs">
                  {BENCH_ARMS.map((a) => (
                    <label key={a} className="flex items-center gap-1">
                      <input
                        type="checkbox"
                        checked={arms.includes(a)}
                        onChange={() =>
                          setArms((prev) =>
                            prev.includes(a) ? prev.filter((x) => x !== a) : [...prev, a],
                          )
                        }
                      />
                      {ARM_LABELS[a]}
                    </label>
                  ))}
                </div>
              </div>
              <div>
                <label className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
                  Council rounds
                </label>
                <div className="mt-1 flex gap-1">
                  {[1, 2, 3].map((r) => (
                    <button
                      key={r}
                      onClick={() => setRounds(r)}
                      className={`flex-1 rounded border px-2 py-0.5 text-xs ${
                        rounds === r
                          ? "border-brand bg-brand text-white"
                          : "border-gray-300 text-gray-600 dark:border-gray-600 dark:text-gray-300"
                      }`}
                    >
                      {r}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          </section>

          <section>
            <label className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
              Prompts (one per line) — use questions you actually care about
            </label>
            <textarea
              value={prompts}
              onChange={(e) => setPrompts(e.target.value)}
              rows={4}
              className="mt-1 w-full rounded border border-gray-300 bg-white px-2 py-1.5 text-xs dark:border-gray-600 dark:bg-gray-800"
            />
          </section>

          <div className="flex items-center gap-3 rounded-lg border border-indigo-200 bg-indigo-50 px-3 py-2 text-xs dark:border-indigo-900 dark:bg-indigo-950">
            <span className="text-indigo-900 dark:text-indigo-200">
              {promptList.length} prompt{promptList.length === 1 ? "" : "s"} ·{" "}
              <strong>~{totalCalls} calls</strong> · roughly{" "}
              {Math.max(1, Math.round((totalCalls * 20) / 60))} min
            </span>
            <button
              onClick={start}
              disabled={running || chosen.length < 2 || !promptList.length}
              className="ml-auto rounded bg-brand px-4 py-1.5 text-xs font-medium text-white hover:brightness-110 disabled:opacity-50"
            >
              {running ? "Running…" : "Run benchmark"}
            </button>
          </div>

          {error && <p className="text-xs text-red-500">{error}</p>}

          {progress.length > 0 && (
            <pre className="max-h-40 overflow-y-auto rounded border border-gray-200 bg-gray-50 p-2 text-[10px] leading-relaxed text-gray-600 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300">
              {progress.join("\n")}
            </pre>
          )}

          {summary && (
            <section className="rounded-xl border-2 border-brand/40 bg-white p-4 dark:bg-gray-900">
              <h3 className="mb-3 text-sm font-semibold text-gray-800 dark:text-gray-100">
                Average judge score over {summary.prompts} prompt
                {summary.prompts === 1 ? "" : "s"}
              </h3>
              <div className="space-y-2">
                {Object.entries(summary.avg_scores).map(([arm, score]) => (
                  <ArmBar key={arm} arm={arm} score={score} best={arm === bestArm} />
                ))}
              </div>
              <div
                className={`mt-4 rounded-lg px-3 py-2 text-xs font-medium ${
                  summary.verdict.startsWith("deliberation wins")
                    ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300"
                    : summary.verdict.startsWith("deliberation is WORSE")
                      ? "bg-rose-100 text-rose-800 dark:bg-rose-950 dark:text-rose-300"
                      : "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300"
                }`}
              >
                {summary.verdict}
              </div>
              <p className="mt-2 text-[10px] text-gray-400">
                One judge over a handful of prompts is a signal, not proof. Re-run with more
                prompts before making a decision you care about.
              </p>
            </section>
          )}

          {rows.length > 0 && (
            <section className="space-y-2">
              <h3 className="text-sm font-medium text-gray-500">Per prompt</h3>
              {rows.map((row, index) => (
                <div
                  key={index}
                  className="rounded-lg border border-gray-200 bg-white p-2 dark:border-gray-700 dark:bg-gray-900"
                >
                  <button
                    onClick={() => setOpenRow(openRow === index ? null : index)}
                    className="w-full text-left"
                  >
                    <div className="truncate text-xs text-gray-700 dark:text-gray-200">
                      {row.prompt}
                    </div>
                    <div className="mt-0.5 flex flex-wrap gap-2 text-[10px] text-gray-500">
                      {Object.entries(row.scores ?? {}).map(([arm, score]) => (
                        <span key={arm}>
                          {ARM_LABELS[arm as BenchArm] ?? arm}: <strong>{score}</strong>
                        </span>
                      ))}
                      <span>· {row.calls} calls</span>
                      {row.council_rounds != null && <span>· {row.council_rounds} rounds</span>}
                    </div>
                  </button>
                  {openRow === index && row.answers && (
                    <div className="mt-2 space-y-3 border-t border-gray-100 pt-2 dark:border-gray-800">
                      {Object.entries(row.answers).map(([arm, text]) => (
                        <details key={arm}>
                          <summary className="cursor-pointer text-[11px] font-semibold text-gray-600 dark:text-gray-300">
                            {ARM_LABELS[arm as BenchArm] ?? arm} — {row.scores?.[arm] ?? "—"}/10
                            {row.reasons?.[arm] ? ` · ${row.reasons[arm]}` : ""}
                          </summary>
                          <div className="mt-1">
                            <MessageRenderer content={text} />
                          </div>
                        </details>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </section>
          )}
        </div>
      </div>
    </div>
  );
}
