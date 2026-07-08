import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch, asUtcDate, streamSSE } from "../api/client";
import { useProviders } from "../hooks/useProviders";
import { ThemeToggle } from "../components/ThemeToggle";
import { useAuth } from "../auth/AuthContext";

interface ModelRef {
  provider_id: string;
  model: string;
}
interface Suite {
  id: string;
  name: string;
  description: string | null;
  system_prompt: string | null;
  prompts: string[];
  models: ModelRef[];
  created_at: string;
  updated_at: string;
}
interface RunResult {
  prompt: string;
  model: string;
  provider: string;
  answer: string;
  error: boolean;
  score: number | null;
  latency_ms: number;
  ttft_ms?: number | null;
  tokens: number;
  tps?: number | null;
}
interface Run {
  id: string;
  created_at: string;
  summary: Record<
    string,
    {
      avg_score: number | null;
      avg_latency_ms: number | null;
      avg_tps?: number | null;
      avg_ttft_ms?: number | null;
      count: number;
    }
  >;
  results: RunResult[];
}

type CellStatus = "queued" | "running" | "scoring" | "done";
interface CellState {
  status: CellStatus;
  provider: string;
  answer: string;
  latency?: number;
  ttft?: number | null;
  tps?: number | null;
  score?: number | null;
  error?: boolean;
}
interface RunInfo {
  prompts: string[];
  models: { model: string; provider: string }[];
  total: number;
}
const cellKey = (pi: number, m: string) => `${pi}\u0000${m}`;

export function EvalsPage() {
  const { logout, user } = useAuth();
  const { data: providers = [] } = useProviders();
  const qc = useQueryClient();
  const { data: suites = [] } = useQuery({
    queryKey: ["eval-suites"],
    queryFn: () => apiFetch<Suite[]>("/api/evals"),
  });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = suites.find((s) => s.id === selectedId) || null;

  const invalidate = () => qc.invalidateQueries({ queryKey: ["eval-suites"] });
  const createSuite = useMutation({
    mutationFn: (body: Partial<Suite>) =>
      apiFetch<Suite>("/api/evals", { method: "POST", body: JSON.stringify(body) }),
    onSuccess: (s) => {
      invalidate();
      setSelectedId(s.id);
    },
  });

  return (
    <div className="flex h-full flex-col bg-gray-50 dark:bg-gray-950">
      <header className="flex items-center justify-between border-b border-gray-200 bg-white px-4 py-2 dark:border-gray-700 dark:bg-gray-900">
        <div className="flex items-center gap-3">
          <Link to="/" className="text-sm text-blue-500">
            ← Chat
          </Link>
          <h1 className="text-lg font-semibold">Evaluations</h1>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-500">{user?.email}</span>
          <ThemeToggle />
          <button
            onClick={logout}
            className="rounded border border-gray-300 px-2 py-1 text-sm dark:border-gray-600"
          >
            Sign out
          </button>
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        {/* Suite list */}
        <div className="w-56 shrink-0 overflow-y-auto border-r border-gray-200 p-2 dark:border-gray-700">
          <button
            onClick={() =>
              createSuite.mutate({
                name: "New suite",
                prompts: [],
                models: [],
              })
            }
            className="mb-2 w-full rounded bg-brand px-2 py-1.5 text-sm font-medium text-white hover:brightness-110"
          >
            + New suite
          </button>
          {suites.map((s) => (
            <button
              key={s.id}
              onClick={() => setSelectedId(s.id)}
              className={`block w-full rounded px-2 py-1.5 text-left text-sm ${
                s.id === selectedId
                  ? "bg-brand/10 text-brand"
                  : "hover:bg-gray-100 dark:hover:bg-gray-800"
              }`}
            >
              <div className="truncate font-medium">{s.name}</div>
              <div className="text-[10px] text-gray-400">
                {s.prompts.length} prompts · {s.models.length} models
              </div>
            </button>
          ))}
          {suites.length === 0 && (
            <p className="px-2 text-xs text-gray-400">
              No suites yet. Create one to run prompts across models.
            </p>
          )}
        </div>

        {/* Editor + results */}
        <div className="min-w-0 flex-1 overflow-y-auto p-4">
          {selected ? (
            <SuiteEditor key={selected.id} suite={selected} providers={providers} />
          ) : (
            <p className="text-sm text-gray-500">
              Select or create a suite. An eval suite runs a set of prompts against
              multiple models and scores each answer with your default (judge) model —
              so you can compare quality and track regressions over time.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

function SuiteEditor({
  suite,
  providers,
}: {
  suite: Suite;
  providers: { id: string; name: string; models: string[] }[];
}) {
  const qc = useQueryClient();
  const [name, setName] = useState(suite.name);
  const [system, setSystem] = useState(suite.system_prompt || "");
  const [promptsText, setPromptsText] = useState(suite.prompts.join("\n"));
  const [models, setModels] = useState<ModelRef[]>(suite.models);
  const [lastRun, setLastRun] = useState<Run | null>(null);
  // Live-run state (streamed from /run/stream).
  const [running, setRunning] = useState(false);
  const [runInfo, setRunInfo] = useState<RunInfo | null>(null);
  const [cells, setCells] = useState<Record<string, CellState>>({});
  const [runError, setRunError] = useState<string | null>(null);
  const ctrlRef = useRef<AbortController | null>(null);
  useEffect(() => () => ctrlRef.current?.abort(), []);

  const prompts = promptsText
    .split("\n")
    .map((p) => p.trim())
    .filter(Boolean);

  const save = useMutation({
    mutationFn: () =>
      apiFetch<Suite>(`/api/evals/${suite.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          name,
          system_prompt: system || null,
          prompts,
          models,
        }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["eval-suites"] }),
  });
  const del = useMutation({
    mutationFn: () => apiFetch<void>(`/api/evals/${suite.id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["eval-suites"] }),
  });

  // Live streaming run: shows each request/response as it happens, then the summary.
  async function runEval() {
    setRunError(null);
    try {
      await save.mutateAsync();
    } catch {
      /* save errors are surfaced via the Save button; keep going */
    }
    setCells({});
    setRunInfo(null);
    setLastRun(null);
    setRunning(true);
    ctrlRef.current?.abort();
    ctrlRef.current = streamSSE(
      `/api/evals/${suite.id}/run/stream`,
      {},
      (evt) => {
        const d: any = evt.data;
        if (evt.event === "run_start") {
          setRunInfo({ prompts: d.prompts, models: d.models, total: d.total });
          const init: Record<string, CellState> = {};
          d.prompts.forEach((_p: string, pi: number) =>
            d.models.forEach((m: { model: string; provider: string }) => {
              init[cellKey(pi, m.model)] = {
                status: "queued",
                provider: m.provider,
                answer: "",
              };
            }),
          );
          setCells(init);
        } else if (evt.event === "cell_start") {
          const k = cellKey(d.prompt_index, d.model);
          setCells((c) => ({
            ...c,
            [k]: {
              ...(c[k] || { provider: d.provider }),
              status: "running",
              answer: "",
            },
          }));
        } else if (evt.event === "cell_token") {
          const k = cellKey(d.prompt_index, d.model);
          setCells((c) => {
            const cur = c[k];
            if (!cur) return c;
            return { ...c, [k]: { ...cur, answer: cur.answer + d.delta } };
          });
        } else if (evt.event === "cell_answer") {
          const k = cellKey(d.prompt_index, d.model);
          setCells((c) => {
            const cur = c[k] || { provider: "", status: "running", answer: "" };
            return {
              ...c,
              [k]: {
                ...cur,
                answer: d.answer,
                latency: d.latency_ms,
                ttft: d.ttft_ms,
                tps: d.tps,
                error: d.error,
              },
            };
          });
        } else if (evt.event === "cell_scoring") {
          const k = cellKey(d.prompt_index, d.model);
          setCells((c) => (c[k] ? { ...c, [k]: { ...c[k], status: "scoring" } } : c));
        } else if (evt.event === "cell_score") {
          const k = cellKey(d.prompt_index, d.model);
          setCells((c) =>
            c[k] ? { ...c, [k]: { ...c[k], score: d.score, status: "done" } } : c,
          );
        } else if (evt.event === "done") {
          setLastRun(d);
          setRunning(false);
          setRunInfo(null);
          setCells({});
          qc.invalidateQueries({ queryKey: ["eval-runs", suite.id] });
        }
      },
      () => setRunning(false),
      (e) => {
        setRunError(e.message);
        setRunning(false);
      },
    );
  }
  const doneCount = useMemo(
    () => Object.values(cells).filter((c) => c.status === "done").length,
    [cells],
  );

  const { data: runs = [] } = useQuery({
    queryKey: ["eval-runs", suite.id],
    queryFn: () => apiFetch<Run[]>(`/api/evals/${suite.id}/runs`),
  });

  const allModels = useMemo(
    () =>
      providers.flatMap((p) =>
        (p.models || []).map((m) => ({ provider_id: p.id, provider: p.name, model: m }))
      ),
    [providers]
  );
  const isSelected = (pid: string, m: string) =>
    models.some((x) => x.provider_id === pid && x.model === m);
  const toggleModel = (pid: string, m: string) =>
    setModels((prev) =>
      isSelected(pid, m)
        ? prev.filter((x) => !(x.provider_id === pid && x.model === m))
        : [...prev, { provider_id: pid, model: m }]
    );
  const providerSelectedCount = (pid: string, ms: string[]) =>
    ms.filter((m) => isSelected(pid, m)).length;
  const toggleProvider = (pid: string, ms: string[]) =>
    setModels((prev) => {
      const allSel = ms.every((m) =>
        prev.some((x) => x.provider_id === pid && x.model === m)
      );
      if (allSel) return prev.filter((x) => x.provider_id !== pid);
      const missing = ms
        .filter((m) => !prev.some((x) => x.provider_id === pid && x.model === m))
        .map((m) => ({ provider_id: pid, model: m }));
      return [...prev, ...missing];
    });

  const activeRun = lastRun || runs[0] || null;

  // Sortable results table. Clicking a numeric column header toggles asc/desc.
  const [sort, setSort] = useState<{
    key: "score" | "latency_ms" | "tps" | "ttft_ms" | null;
    dir: "asc" | "desc";
  }>({ key: null, dir: "desc" });
  const toggleSort = (key: "score" | "latency_ms" | "tps" | "ttft_ms") =>
    setSort((s) =>
      s.key === key
        ? { key, dir: s.dir === "asc" ? "desc" : "asc" }
        : { key, dir: "desc" },
    );
  const sortedResults = useMemo(() => {
    const rows = activeRun?.results ? [...activeRun.results] : [];
    if (!sort.key) return rows;
    const key = sort.key;
    const mul = sort.dir === "asc" ? 1 : -1;
    return rows.sort((a, b) => {
      const av = (a as any)[key];
      const bv = (b as any)[key];
      // Push null/undefined to the bottom regardless of direction.
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      return (av - bv) * mul;
    });
  }, [activeRun, sort]);
  const sortArrow = (key: "score" | "latency_ms" | "tps" | "ttft_ms") =>
    sort.key === key ? (sort.dir === "asc" ? " ▲" : " ▼") : "";

  return (
    <div className="mx-auto max-w-5xl space-y-4">
      <div className="flex items-center gap-2">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          className="flex-1 rounded border border-gray-300 px-2 py-1 text-lg font-semibold dark:border-gray-600 dark:bg-gray-800"
        />
        <button
          onClick={() => save.mutate()}
          className="rounded border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-600"
        >
          {save.isPending ? "Saving…" : "Save"}
        </button>
        <button
          onClick={runEval}
          disabled={running || prompts.length === 0 || models.length === 0}
          className="rounded bg-brand px-3 py-1.5 text-sm font-medium text-white hover:brightness-110 disabled:opacity-40"
        >
          {running
            ? `Running… ${doneCount}/${runInfo?.total ?? prompts.length * models.length}`
            : "▶ Run eval"}
        </button>
        <button
          onClick={() => confirm("Delete this suite?") && del.mutate()}
          className="rounded border border-gray-300 px-2 py-1.5 text-sm text-gray-400 hover:text-red-500 dark:border-gray-600"
        >
          Delete
        </button>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div>
          <label className="mb-1 block text-xs font-medium text-gray-500">
            Prompts (one per line) · {prompts.length}
          </label>
          <textarea
            value={promptsText}
            onChange={(e) => setPromptsText(e.target.value)}
            rows={8}
            placeholder="What is idempotency?&#10;Write a bash script to tail logs&#10;…"
            className="w-full resize-y rounded border border-gray-300 px-2 py-1 text-sm dark:border-gray-600 dark:bg-gray-800"
          />
          <label className="mb-1 mt-3 block text-xs font-medium text-gray-500">
            System prompt (optional)
          </label>
          <textarea
            value={system}
            onChange={(e) => setSystem(e.target.value)}
            rows={3}
            className="w-full resize-y rounded border border-gray-300 px-2 py-1 text-sm dark:border-gray-600 dark:bg-gray-800"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-gray-500">
            Models · {models.length} selected
          </label>
          <div className="max-h-64 overflow-y-auto rounded border border-gray-200 p-2 dark:border-gray-700">
            {providers.map((p) => {
              const pmodels = p.models || [];
              if (pmodels.length === 0) return null;
              const selCount = providerSelectedCount(p.id, pmodels);
              const allSel = selCount === pmodels.length;
              const someSel = selCount > 0 && !allSel;
              return (
                <div key={p.id} className="mb-1.5">
                  <label className="flex items-center gap-2 py-0.5 text-xs font-semibold text-gray-600 dark:text-gray-300">
                    <input
                      type="checkbox"
                      checked={allSel}
                      ref={(el) => {
                        if (el) el.indeterminate = someSel;
                      }}
                      onChange={() => toggleProvider(p.id, pmodels)}
                    />
                    {p.name}
                    <span className="font-normal text-gray-400">
                      · {selCount}/{pmodels.length}
                    </span>
                  </label>
                  <div className="ml-5">
                    {pmodels.map((m) => (
                      <label
                        key={`${p.id}:${m}`}
                        className="flex items-center gap-2 py-0.5 text-xs"
                      >
                        <input
                          type="checkbox"
                          checked={isSelected(p.id, m)}
                          onChange={() => toggleModel(p.id, m)}
                        />
                        {m}
                      </label>
                    ))}
                  </div>
                </div>
              );
            })}
            {allModels.length === 0 && (
              <p className="text-xs text-gray-400">No provider models available.</p>
            )}
          </div>
        </div>
      </div>

      {runError && <p className="text-sm text-red-500">⚠️ {runError}</p>}

      {running && runInfo && (
        <section className="space-y-3">
          <div className="flex items-center gap-3">
            <svg
              className="h-4 w-4 shrink-0 animate-spin text-brand"
              viewBox="0 0 24 24"
              fill="none"
            >
              <circle
                className="opacity-25"
                cx="12"
                cy="12"
                r="10"
                stroke="currentColor"
                strokeWidth="4"
              />
              <path
                className="opacity-75"
                fill="currentColor"
                d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
              />
            </svg>
            <h2 className="text-sm font-medium">
              Running eval — {doneCount}/{runInfo.total} done
            </h2>
            <div className="h-1.5 flex-1 rounded-full bg-gray-200 dark:bg-gray-700">
              <div
                className="h-full rounded-full bg-brand transition-all"
                style={{
                  width: `${runInfo.total ? (doneCount / runInfo.total) * 100 : 0}%`,
                }}
              />
            </div>
          </div>

          {runInfo.prompts.map((prompt, pi) => (
            <div
              key={pi}
              className="overflow-hidden rounded-lg border border-gray-200 dark:border-gray-700"
            >
              <div className="border-b border-gray-200 bg-gray-50 px-3 py-1.5 text-xs dark:border-gray-700 dark:bg-gray-800/50">
                <span className="font-medium text-gray-500">Prompt {pi + 1}:</span>{" "}
                <span className="text-gray-700 dark:text-gray-300">{prompt}</span>
              </div>
              <div className="divide-y divide-gray-100 dark:divide-gray-800">
                {runInfo.models.map((m) => {
                  const cell = cells[cellKey(pi, m.model)];
                  const status = cell?.status ?? "queued";
                  return (
                    <div key={m.model} className="px-3 py-2 text-xs">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="w-3 shrink-0 text-center">
                          {status === "done" ? (
                            cell?.error ? "⚠️" : "✓"
                          ) : status === "queued" ? (
                            <span className="text-gray-300">•</span>
                          ) : (
                            <span className="inline-block animate-spin">◜</span>
                          )}
                        </span>
                        <span className="font-medium">{m.model}</span>
                        <span className="text-gray-400">{m.provider}</span>
                        {status === "running" && (
                          <span className="text-brand">generating…</span>
                        )}
                        {status === "scoring" && (
                          <span className="text-amber-500">scoring…</span>
                        )}
                        {cell?.latency != null && (
                          <span className="text-gray-400 tabular-nums">
                            · {cell.latency} ms
                          </span>
                        )}
                        {cell?.ttft != null && (
                          <span className="text-gray-400 tabular-nums">
                            · {cell.ttft} ms TTFT
                          </span>
                        )}
                        {cell?.tps != null && (
                          <span className="text-gray-400 tabular-nums">
                            · {cell.tps} tok/s
                          </span>
                        )}
                        {cell?.score != null && (
                          <span
                            className={`rounded px-1.5 py-0.5 font-medium ${
                              cell.score >= 8
                                ? "bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300"
                                : cell.score >= 5
                                  ? "bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300"
                                  : "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300"
                            }`}
                          >
                            {cell.score}/10
                          </span>
                        )}
                      </div>
                      {cell?.answer && (
                        <div
                          className={`mt-1 max-h-40 overflow-y-auto whitespace-pre-wrap rounded p-2 text-[11px] ${
                            cell.error
                              ? "bg-red-50 text-red-600 dark:bg-red-950/30 dark:text-red-300"
                              : "bg-gray-50 text-gray-600 dark:bg-gray-800 dark:text-gray-300"
                          }`}
                        >
                          {cell.answer}
                          {status === "running" && (
                            <span className="ml-0.5 animate-pulse">▍</span>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
        </section>
      )}

      {!running && activeRun && (
        <section>
          <h2 className="mb-2 text-sm font-medium text-gray-500">
            Latest results{" "}
            <span className="font-normal text-gray-400">
              · scored 1–10 by judge model
            </span>
          </h2>
          <div className="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-700">
            <table className="w-full text-xs">
              <thead className="bg-gray-50 text-left text-gray-500 dark:bg-gray-800/50">
                <tr>
                  <th className="px-3 py-2 font-medium">Prompt</th>
                  <th className="px-3 py-2 font-medium">Model</th>
                  <th className="px-3 py-2 font-medium">
                    <button
                      onClick={() => toggleSort("score")}
                      className="font-medium hover:text-gray-700 dark:hover:text-gray-200"
                    >
                      Score{sortArrow("score")}
                    </button>
                  </th>
                  <th className="px-3 py-2 font-medium">
                    <button
                      onClick={() => toggleSort("latency_ms")}
                      className="font-medium hover:text-gray-700 dark:hover:text-gray-200"
                    >
                      Latency{sortArrow("latency_ms")}
                    </button>
                  </th>
                  <th className="px-3 py-2 font-medium">
                    <button
                      onClick={() => toggleSort("ttft_ms")}
                      className="font-medium hover:text-gray-700 dark:hover:text-gray-200"
                    >
                      TTFT{sortArrow("ttft_ms")}
                    </button>
                  </th>
                  <th className="px-3 py-2 font-medium">
                    <button
                      onClick={() => toggleSort("tps")}
                      className="font-medium hover:text-gray-700 dark:hover:text-gray-200"
                    >
                      Tok/s{sortArrow("tps")}
                    </button>
                  </th>
                  <th className="px-3 py-2 font-medium">Answer</th>
                </tr>
              </thead>
              <tbody>
                {sortedResults.map((r, i) => (
                  <tr key={i} className="border-t border-gray-100 align-top dark:border-gray-800">
                    <td className="max-w-[180px] truncate px-3 py-2" title={r.prompt}>
                      {r.prompt}
                    </td>
                    <td className="px-3 py-2">{r.model}</td>
                    <td className="px-3 py-2">
                      {r.score != null ? (
                        <span
                          className={`rounded px-1.5 py-0.5 font-medium ${
                            r.score >= 8
                              ? "bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300"
                              : r.score >= 5
                                ? "bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300"
                                : "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300"
                          }`}
                        >
                          {r.score}/10
                        </span>
                      ) : r.error ? (
                        <span className="text-red-500">err</span>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="px-3 py-2 tabular-nums">{r.latency_ms} ms</td>
                    <td className="px-3 py-2 tabular-nums">
                      {r.ttft_ms != null ? `${r.ttft_ms} ms` : "—"}
                    </td>
                    <td className="px-3 py-2 tabular-nums">
                      {r.tps != null ? `${r.tps} tok/s` : "—"}
                    </td>
                    <td className="max-w-[320px] truncate px-3 py-2" title={r.answer}>
                      {r.answer}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {runs.length > 0 && (
        <section>
          <h2 className="mb-2 text-sm font-medium text-gray-500">
            Regression tracking · avg score per model across runs
          </h2>
          <div className="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-700">
            <table className="w-full text-xs">
              <thead className="bg-gray-50 text-left text-gray-500 dark:bg-gray-800/50">
                <tr>
                  <th className="px-3 py-2 font-medium">Run</th>
                  {Array.from(
                    new Set(runs.flatMap((r) => Object.keys(r.summary)))
                  ).map((m) => (
                    <th key={m} className="px-3 py-2 font-medium">
                      {m}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => {
                  const cols = Array.from(
                    new Set(runs.flatMap((x) => Object.keys(x.summary)))
                  );
                  return (
                    <tr key={r.id} className="border-t border-gray-100 dark:border-gray-800">
                      <td className="px-3 py-2 text-gray-400">
                        {asUtcDate(r.created_at).toLocaleString()}
                      </td>
                      {cols.map((m) => (
                        <td key={m} className="px-3 py-2 tabular-nums">
                          {r.summary[m]?.avg_score != null
                            ? r.summary[m].avg_score
                            : "—"}
                        </td>
                      ))}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  );
}
