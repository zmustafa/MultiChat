import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router";
import { apiFetch, mediaUrl } from "../api/client";
import type { Attachment } from "../api/types";
import { useProviders } from "../hooks/useProviders";
import { usePersonas } from "../hooks/usePersonas";
import { resolvePersonaLanes } from "../utils/personaLanes";
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
  model: string;
}

/** Rough per-call cost used only for the up-front estimate. */
function estimateCost(calls: number): string {
  return `$${(calls * 0.025).toFixed(2)}`;
}

/**
 * The "ask a panel" screen, rendered where the deliberation itself will appear.
 *
 * A deliberation needs its question before it can start, which is what the old modal
 * existed for. Here the question box is the page instead: a persona supplies the panel
 * and the settings, so the common path is type-and-go, with everything still adjustable
 * for this one run without editing the persona.
 */
export function DeliberationLaunch({ personaId }: { personaId: string | null }) {
  const navigate = useNavigate();
  const { data: providers = [] } = useProviders();
  const { data: personas = [] } = usePersonas();
  const persona = personas.find((p) => p.id === personaId) || null;

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
  const [mode, setMode] = useState<"council" | "quick">("council");
  const [evidence, setEvidence] = useState(false);
  const [synthesis, setSynthesis] = useState(true);
  const [critiqueSynthesis, setCritiqueSynthesis] = useState(true);
  const [prompt, setPrompt] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [showAdjust, setShowAdjust] = useState(false);
  const [dropped, setDropped] = useState<string[]>([]);
  const [hint, setHint] = useState<{
    complexity: string;
    recommend: string;
    reason: string;
  } | null>(null);

  // Apply the persona once providers are known: its lanes become the panel and its
  // preset the settings. Without a persona, fall back to a diverse three-model panel.
  const applied = persona?.id ?? (options.length ? "__none__" : "");
  useEffect(() => {
    if (!applied || !options.length) return;
    if (persona) {
      const resolved = resolvePersonaLanes(persona, providers);
      const keys = options.map((o) => o.key);
      const panel = resolved
        .filter((l) => l.role !== "judge")
        .map((l) => `${l.provider_id}::${l.model}`)
        .filter((k) => keys.includes(k));
      const judgeLane = resolved.find((l) => l.role === "judge");
      const judge = judgeLane ? `${judgeLane.provider_id}::${judgeLane.model}` : "";

      const wanted = persona.lanes.filter((l) => l.role !== "judge").map((l) => l.model);
      const bound = new Set(
        resolved.filter((l) => l.role !== "judge").map((l) => l.model),
      );
      setDropped(wanted.filter((m) => !bound.has(m)));

      setSelected(panel.slice(0, MAX_PANEL));
      setJudgeKey(keys.includes(judge) ? judge : "");
      const d = persona.deliberation;
      if (d) {
        setMode(d.mode);
        setRounds(d.max_rounds);
        setSynthesis(d.synthesis);
        setCritiqueSynthesis(d.critique_synthesis);
        setEvidence(d.evidence);
      }
      return;
    }
    // No persona: preselect three models, preferring distinct providers.
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
    setShowAdjust(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [applied, options.length]);

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
  const judgeOnPanel = !!judge && chosen.some((c) => c.model === judge.model);
  const providerIds = new Set(chosen.map((c) => c.providerId));
  const sameLab = chosen.length > 1 && providerIds.size < chosen.length;

  const calls =
    mode === "quick"
      ? chosen.length * 2
      : chosen.length * (1 + rounds) + (synthesis ? 1 : 0) + (critiqueSynthesis ? 2 : 0);

  function toggle(key: string) {
    setSelected((prev) =>
      prev.includes(key)
        ? prev.filter((k) => k !== key)
        : prev.length >= MAX_PANEL
          ? prev
          : [...prev, key],
    );
  }

  async function uploadFiles(files: File[]) {
    const images = files.filter((f) => f.type.startsWith("image/"));
    if (!images.length) return;
    setUploading(true);
    try {
      const form = new FormData();
      images.forEach((f) => form.append("files", f));
      const res = await apiFetch<Attachment[]>("/api/uploads", {
        method: "POST",
        body: form,
      });
      setAttachments((prev) => [...prev, ...res]);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
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
        mode,
        evidence,
        attachment_ids: attachments.map((a) => a.id),
      });
      navigate(`/d/${res.run_id}`);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }

  const summary = [
    mode === "quick" ? "Quick vote" : `Council · ${rounds} round${rounds === 1 ? "" : "s"}`,
    synthesis ? "synthesis" : null,
    critiqueSynthesis && synthesis ? "audited" : null,
    evidence ? "evidence required" : null,
  ].filter(Boolean);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <header className="flex items-center gap-2 border-b border-gray-200 px-3 py-2 dark:border-gray-700">
        <span className="truncate text-sm font-semibold text-gray-800 dark:text-gray-100">
          {/* Preset names already carry the scales, so don't double it up. */}
          ⚖️ {persona ? persona.name.replace(/^⚖️\s*/, "") : "New deliberation"}
        </span>
        {persona?.description && (
          <span className="truncate text-[11px] text-gray-400">{persona.description}</span>
        )}
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        <div className="mx-auto max-w-3xl space-y-3">
          <div>
            <label className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
              Your question
            </label>
            <div
              onDragOver={(e) => {
                e.preventDefault();
                setDragOver(true);
              }}
              onDragLeave={(e) => {
                if (!e.currentTarget.contains(e.relatedTarget as Node)) setDragOver(false);
              }}
              onDrop={(e) => {
                e.preventDefault();
                setDragOver(false);
                void uploadFiles(Array.from(e.dataTransfer.files));
              }}
              className={`relative mt-1 rounded-lg border ${
                dragOver ? "border-brand border-dashed" : "border-gray-300 dark:border-gray-600"
              } dark:bg-gray-800`}
            >
              {attachments.length > 0 && (
                <div className="flex flex-wrap gap-2 p-2 pb-0">
                  {attachments.map((a) => (
                    <div key={a.id} className="relative">
                      <img
                        src={mediaUrl(a.url)}
                        alt={a.filename}
                        className="h-16 rounded border border-gray-200 object-cover dark:border-gray-700"
                      />
                      <button
                        onClick={() =>
                          setAttachments((prev) => prev.filter((x) => x.id !== a.id))
                        }
                        title="Remove"
                        className="absolute -right-1.5 -top-1.5 rounded-full bg-gray-700 px-1 text-[10px] text-white"
                      >
                        ×
                      </button>
                    </div>
                  ))}
                </div>
              )}
              <textarea
                autoFocus
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                onPaste={(e) => {
                  const files = Array.from(e.clipboardData?.items || [])
                    .filter((i) => i.kind === "file")
                    .map((i) => i.getAsFile())
                    .filter((f): f is File => !!f);
                  if (files.length) void uploadFiles(files);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void start();
                }}
                rows={5}
                placeholder="Ask something where reasonable experts could disagree…"
                className="w-full resize-none bg-transparent px-3 py-2 text-sm focus:outline-none"
              />
              {dragOver && (
                <div className="pointer-events-none absolute inset-0 flex items-center justify-center rounded-lg bg-brand/5 text-xs text-brand">
                  Drop images to attach
                </div>
              )}
            </div>
            <div className="mt-1 flex items-center gap-2 text-[11px] text-gray-400">
              <input
                ref={fileRef}
                type="file"
                accept="image/*"
                multiple
                hidden
                onChange={(e) => void uploadFiles(Array.from(e.target.files ?? []))}
              />
              <button
                onClick={() => fileRef.current?.click()}
                disabled={uploading}
                className="rounded border border-gray-300 px-2 py-0.5 text-gray-600 hover:bg-gray-50 disabled:opacity-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
              >
                {uploading ? "Uploading…" : "📎 Add image"}
              </button>
              <span>
                {navigator.platform.includes("Mac") ? "⌘" : "Ctrl"}+Enter to start
                {attachments.length > 0 &&
                  ` · every panelist and reviewer sees ${
                    attachments.length === 1 ? "the image" : "all images"
                  }`}
              </span>
            </div>
          </div>

          {hint && hint.recommend !== "deliberate" && (
            <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200">
              This looks like a <strong>{hint.complexity}</strong> question — {hint.reason} A
              panel costs more and rarely beats a single good answer here.
            </div>
          )}

          <div className="rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 dark:border-gray-700 dark:bg-gray-900">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
                Panel ({chosen.length})
              </span>
              {chosen.map((c) => (
                <span
                  key={c.key}
                  className="rounded-full bg-white px-2 py-0.5 text-[11px] text-gray-700 shadow-sm dark:bg-gray-800 dark:text-gray-200"
                >
                  {c.model}
                </span>
              ))}
              {judge && (
                <span className="rounded-full bg-indigo-100 px-2 py-0.5 text-[11px] text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300">
                  judge: {judge.model}
                </span>
              )}
              <button
                onClick={() => setShowAdjust((v) => !v)}
                className="ml-auto rounded border border-gray-300 px-2 py-0.5 text-[11px] text-gray-600 hover:bg-white dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
              >
                {showAdjust ? "Done" : "Adjust…"}
              </button>
            </div>
            <div className="mt-1 text-[11px] text-gray-500">
              {summary.join(" · ")} — <strong>{calls} calls</strong> · {estimateCost(calls)}
            </div>
            {dropped.length > 0 && (
              <div className="mt-1 text-[11px] text-amber-600 dark:text-amber-400">
                No configured provider offers {dropped.join(", ")} — dropped from the panel.
              </div>
            )}
            {sameLab && (
              <div className="mt-1 text-[11px] text-amber-600 dark:text-amber-400">
                Some panelists share a provider — shared blind spots survive debate.
              </div>
            )}
            {judgeOnPanel && (
              <div className="mt-1 text-[11px] text-amber-600 dark:text-amber-400">
                The judge is also on the panel and will be grading its own answer.
              </div>
            )}
          </div>

          {showAdjust && (
            <div className="space-y-3 rounded-lg border border-gray-200 p-3 dark:border-gray-700">
              <div>
                <label className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
                  Mode
                </label>
                <div className="mt-1 grid grid-cols-2 gap-2">
                  {(
                    [
                      ["quick", "Quick", "Everyone answers, then the panel votes."],
                      ["council", "Council", "Full peer review over rounds, then a synthesis."],
                    ] as const
                  ).map(([value, label, blurb]) => (
                    <button
                      key={value}
                      onClick={() => setMode(value)}
                      className={`rounded-lg border p-2 text-left text-xs ${
                        mode === value
                          ? "border-brand bg-brand/5"
                          : "border-gray-300 dark:border-gray-600"
                      }`}
                    >
                      <div className="font-semibold text-gray-800 dark:text-gray-100">{label}</div>
                      <div className="text-[11px] text-gray-500">{blurb}</div>
                    </button>
                  ))}
                </div>
              </div>

              <div>
                <label className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
                  Panel — pick 2 to {MAX_PANEL}
                </label>
                <div className="mt-1 max-h-44 overflow-y-auto rounded border border-gray-200 dark:border-gray-700">
                  {options.map((o) => (
                    <label
                      key={o.key}
                      className="flex items-center gap-2 px-2 py-1 text-xs hover:bg-gray-50 dark:hover:bg-gray-800"
                    >
                      <input
                        type="checkbox"
                        checked={selected.includes(o.key)}
                        onChange={() => toggle(o.key)}
                      />
                      <span className="text-gray-800 dark:text-gray-100">{o.model}</span>
                      <span className="text-gray-400">{o.providerName}</span>
                    </label>
                  ))}
                </div>
              </div>

              <div className="flex flex-wrap items-end gap-4">
                <div>
                  <label className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
                    Judge / synthesizer
                  </label>
                  <select
                    value={judgeKey}
                    onChange={(e) => setJudgeKey(e.target.value)}
                    className="mt-1 block rounded border border-gray-300 px-2 py-1 text-xs dark:border-gray-600 dark:bg-gray-800"
                  >
                    <option value="">Auto — the panel's runner-up</option>
                    {options.map((o) => (
                      <option key={o.key} value={o.key}>
                        {o.model} ({o.providerName})
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
                    Review rounds
                  </label>
                  <div className="mt-1 flex gap-1">
                    {[1, 2, 3].map((r) => (
                      <button
                        key={r}
                        disabled={mode === "quick"}
                        onClick={() => setRounds(r)}
                        className={`rounded border px-3 py-1 text-xs disabled:opacity-40 ${
                          rounds === r && mode !== "quick"
                            ? "border-brand bg-brand text-white"
                            : "border-gray-300 dark:border-gray-600"
                        }`}
                      >
                        {r}
                      </button>
                    ))}
                  </div>
                </div>
              </div>

              <div className="flex flex-wrap gap-4 text-xs text-gray-700 dark:text-gray-200">
                <label className="flex items-center gap-1.5">
                  <input
                    type="checkbox"
                    checked={synthesis}
                    disabled={mode === "quick"}
                    onChange={(e) => setSynthesis(e.target.checked)}
                  />
                  Synthesis + minority report
                </label>
                <label className="flex items-center gap-1.5">
                  <input
                    type="checkbox"
                    checked={critiqueSynthesis}
                    disabled={mode === "quick" || !synthesis}
                    onChange={(e) => setCritiqueSynthesis(e.target.checked)}
                  />
                  Audit the synthesis
                </label>
                <label className="flex items-center gap-1.5">
                  <input
                    type="checkbox"
                    checked={evidence}
                    onChange={(e) => setEvidence(e.target.checked)}
                  />
                  Require evidence on facts
                </label>
              </div>

              {persona && (
                <div className="text-[11px] text-gray-400">
                  Changes apply to this run only — “{persona.name}” is unchanged.
                </div>
              )}
            </div>
          )}

          {error && (
            <div className="rounded border border-rose-300 bg-rose-50 px-3 py-2 text-xs text-rose-700 dark:border-rose-800 dark:bg-rose-950 dark:text-rose-300">
              {error}
            </div>
          )}

          <button
            onClick={start}
            disabled={busy || !prompt.trim() || chosen.length < 2}
            className="rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
          >
            {busy ? "Starting…" : "Start deliberation"}
          </button>
        </div>
      </div>
    </div>
  );
}
