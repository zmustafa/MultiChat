import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router";
import { useProviders } from "../hooks/useProviders";
import { useDismiss } from "../hooks/useDismiss";
import {
  classifyPrompt,
  createDeliberation,
  type PanelMember,
} from "../api/deliberation";

const MAX_PANEL = 5;

interface Option {
  key: string;
  providerId: string;
  providerName: string;
  providerType: string;
  model: string;
}

/** Rough per-call cost used only for the up-front estimate. */
function estimateCost(calls: number): string {
  const perCall = 0.025;
  return `$${(calls * perCall).toFixed(2)}`;
}

export function DeliberationSetup({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate();
  const { data: providers = [] } = useProviders();
  const boxRef = useRef<HTMLDivElement>(null);
  useDismiss(boxRef, true, onClose);

  const options = useMemo<Option[]>(() => {
    const out: Option[] = [];
    for (const p of providers) {
      const models = p.models?.length ? p.models : p.default_model ? [p.default_model] : [];
      for (const m of models) {
        out.push({
          key: `${p.id}::${m}`,
          providerId: p.id,
          providerName: p.name,
          providerType: p.provider_type,
          model: m,
        });
      }
    }
    return out;
  }, [providers]);

  const [selected, setSelected] = useState<string[]>([]);
  const [judgeKey, setJudgeKey] = useState<string>("");
  const [rounds, setRounds] = useState(2);
  const [synthesis, setSynthesis] = useState(true);
  const [critiqueSynthesis, setCritiqueSynthesis] = useState(true);
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [hint, setHint] = useState<{ complexity: string; recommend: string; reason: string } | null>(
    null,
  );

  // Preselect a diverse panel: three different models, preferring distinct providers.
  useEffect(() => {
    if (selected.length || !options.length) return;
    const picked: string[] = [];
    const usedProviders = new Set<string>();
    for (const o of options) {
      if (picked.length >= 3) break;
      if (usedProviders.has(o.providerId)) continue;
      usedProviders.add(o.providerId);
      picked.push(o.key);
    }
    for (const o of options) {
      if (picked.length >= 3) break;
      if (!picked.includes(o.key)) picked.push(o.key);
    }
    setSelected(picked);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [options]);

  // Deliberation can make simple factual answers worse, so warn before spending on one.
  useEffect(() => {
    const text = prompt.trim();
    if (text.length < 25) {
      setHint(null);
      return;
    }
    const timer = setTimeout(() => {
      classifyPrompt(text)
        .then(setHint)
        .catch(() => setHint(null));
    }, 900);
    return () => clearTimeout(timer);
  }, [prompt]);

  const chosen = options.filter((o) => selected.includes(o.key));
  const judge = options.find((o) => o.key === judgeKey) || null;
  const panelModels = new Set(chosen.map((c) => c.model));
  const judgeOnPanel = !!judge && panelModels.has(judge.model);
  const providerIds = new Set(chosen.map((c) => c.providerId));
  const sameLab = chosen.length > 1 && providerIds.size < chosen.length;

  const calls = chosen.length * (1 + rounds) + (synthesis ? 1 : 0) + (critiqueSynthesis ? 2 : 0);

  function toggle(key: string) {
    setSelected((prev) =>
      prev.includes(key)
        ? prev.filter((k) => k !== key)
        : prev.length >= MAX_PANEL
          ? prev
          : [...prev, key],
    );
  }

  async function start() {
    setError("");
    if (chosen.length < 2) return setError("Pick at least two models for the panel.");
    if (!prompt.trim()) return setError("Enter a question.");
    setBusy(true);
    try {
      const participants: PanelMember[] = chosen.map((c) => ({
        provider_id: c.providerId,
        model: c.model,
      }));
      const res = await createDeliberation({
        prompt: prompt.trim(),
        participants,
        judge: judge ? { provider_id: judge.providerId, model: judge.model } : null,
        max_rounds: rounds,
        synthesis,
        minority_report: true,
        critique_synthesis: critiqueSynthesis,
      });
      onClose();
      navigate(`/d/${res.run_id}`);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/40 p-6">
      <div
        ref={boxRef}
        className="w-full max-w-2xl rounded-xl border border-gray-200 bg-white shadow-2xl dark:border-gray-700 dark:bg-gray-900"
      >
        <div className="flex items-center justify-between border-b border-gray-200 px-5 py-3 dark:border-gray-700">
          <h2 className="text-sm font-semibold text-gray-800 dark:text-gray-100">
            ⚖️ New deliberation
          </h2>
          <button
            onClick={onClose}
            className="rounded px-2 text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800"
          >
            ✕
          </button>
        </div>

        <div className="space-y-4 px-5 py-4">
          <section>
            <div className="mb-1 flex items-baseline justify-between">
              <label className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
                Panel ({chosen.length}/{MAX_PANEL})
              </label>
              <span className="text-[10px] text-gray-400">
                prefer different providers — shared blind spots survive debate
              </span>
            </div>
            <div className="max-h-44 space-y-0.5 overflow-y-auto rounded border border-gray-200 p-1 dark:border-gray-700">
              {options.map((o) => (
                <label
                  key={o.key}
                  className="flex cursor-pointer items-center gap-2 rounded px-2 py-1 text-xs hover:bg-gray-50 dark:hover:bg-gray-800"
                >
                  <input
                    type="checkbox"
                    checked={selected.includes(o.key)}
                    onChange={() => toggle(o.key)}
                  />
                  <span className="font-medium text-gray-800 dark:text-gray-100">{o.model}</span>
                  <span className="text-gray-400">{o.providerName}</span>
                </label>
              ))}
              {!options.length && (
                <div className="p-2 text-xs text-gray-500">No models configured.</div>
              )}
            </div>
            {sameLab && (
              <p className="mt-1 text-[11px] text-amber-600 dark:text-amber-400">
                ⚠ Two panelists share a provider — a cross-provider panel catches more
                shared errors.
              </p>
            )}
          </section>

          <section className="grid grid-cols-2 gap-4">
            <div>
              <label className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
                Judge / synthesizer
              </label>
              <select
                value={judgeKey}
                onChange={(e) => setJudgeKey(e.target.value)}
                className="mt-1 w-full rounded border border-gray-300 bg-white px-2 py-1 text-xs dark:border-gray-600 dark:bg-gray-800"
              >
                <option value="">Auto — the panel's runner-up</option>
                {options.map((o) => (
                  <option key={o.key} value={o.key}>
                    {o.model} · {o.providerName}
                  </option>
                ))}
              </select>
              {judgeOnPanel ? (
                <p className="mt-1 text-[11px] text-amber-600 dark:text-amber-400">
                  ⚠ This model is on the panel — judges favour their own contributions.
                </p>
              ) : (
                judge && (
                  <p className="mt-1 text-[11px] text-green-600 dark:text-green-400">
                    ✓ Not on the panel
                  </p>
                )
              )}
            </div>
            <div>
              <label className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
                Review rounds
              </label>
              <div className="mt-1 flex gap-1">
                {[1, 2, 3].map((r) => (
                  <button
                    key={r}
                    onClick={() => setRounds(r)}
                    className={`flex-1 rounded border px-2 py-1 text-xs ${
                      rounds === r
                        ? "border-brand bg-brand text-white"
                        : "border-gray-300 text-gray-600 dark:border-gray-600 dark:text-gray-300"
                    }`}
                  >
                    {r}
                  </button>
                ))}
              </div>
              {rounds >= 3 && (
                <p className="mt-1 text-[11px] text-amber-600 dark:text-amber-400">
                  ⚠ Models agree more the longer they talk — 2 rounds is usually the
                  sweet spot.
                </p>
              )}
            </div>
          </section>

          <section className="flex gap-4 text-xs text-gray-700 dark:text-gray-200">
            <label className="flex items-center gap-1.5">
              <input
                type="checkbox"
                checked={synthesis}
                onChange={(e) => setSynthesis(e.target.checked)}
              />
              Synthesis + minority report
            </label>
            <label className="flex items-center gap-1.5">
              <input
                type="checkbox"
                checked={critiqueSynthesis}
                onChange={(e) => setCritiqueSynthesis(e.target.checked)}
                disabled={!synthesis}
              />
              Audit the synthesis
            </label>
          </section>

          <section className="rounded-lg border border-indigo-200 bg-indigo-50 px-3 py-2 text-xs dark:border-indigo-900 dark:bg-indigo-950">
            <div className="font-medium text-indigo-900 dark:text-indigo-200">
              {chosen.length} model{chosen.length === 1 ? "" : "s"} × {1 + rounds} pass
              {rounds === 0 ? "" : "es"}
              {synthesis ? " + synthesis" : ""} = <strong>{calls} calls</strong> ·{" "}
              {estimateCost(calls)}
            </div>
            <div className="text-indigo-700/80 dark:text-indigo-300/80">
              roughly {Math.max(1, Math.round((calls * 18) / 60))}–
              {Math.max(2, Math.round((calls * 30) / 60))} min
            </div>
          </section>

          <section>
            <label className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
              Your question
            </label>
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              rows={4}
              placeholder="Ask something where reasonable experts could disagree…"
              className="mt-1 w-full rounded border border-gray-300 bg-white px-2 py-1.5 text-sm focus:border-brand focus:outline-none dark:border-gray-600 dark:bg-gray-800"
            />
            {hint && hint.recommend === "single" && (
              <p className="mt-1 text-[11px] text-amber-600 dark:text-amber-400">
                ⓘ This looks like a {hint.complexity} question — a single model is
                probably enough, and panels can over-correct on simple facts.
                {hint.reason ? ` (${hint.reason})` : ""}
              </p>
            )}
          </section>

          {error && <p className="text-xs text-red-500">{error}</p>}
        </div>

        <div className="flex justify-end gap-2 border-t border-gray-200 px-5 py-3 dark:border-gray-700">
          <button
            onClick={onClose}
            className="rounded border border-gray-300 px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
          >
            Cancel
          </button>
          <button
            onClick={start}
            disabled={busy || chosen.length < 2 || !prompt.trim()}
            className="rounded bg-brand px-4 py-1.5 text-xs font-medium text-white hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy ? "Starting…" : "Start deliberation"}
          </button>
        </div>
      </div>
    </div>
  );
}
