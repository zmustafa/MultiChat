import { useMemo, useState } from "react";
import type { Lane, LaneMessage, Provider, Turn } from "../api/types";
import type { LiveMap } from "../hooks/useBroadcast";
import { apiFetch } from "../api/client";
import { addArtifact } from "../hooks/useArtifacts";
import { MessageRenderer } from "./MessageRenderer";

interface Props {
  sessionId: string;
  lanes: Lane[];
  providers: Provider[];
  messages: LaneMessage[];
  live: LiveMap;
  latestTurn?: Turn | null;
  onClose: () => void;
}

interface Row {
  laneId: string;
  model: string;
  chars: number;
  tokPerSec: number | null;
  latencyMs: number | null;
}

/** Cross-lane insights for the latest turn: fastest, longest, and synthesize. */
export function InsightsPanel({
  sessionId,
  lanes,
  messages,
  latestTurn,
  onClose,
}: Props) {
  const [synth, setSynth] = useState<{
    content: string;
    provider: string;
    model: string;
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const rows: Row[] = useMemo(() => {
    if (!latestTurn) return [];
    const responders = lanes
      .filter((l) => l.role === "responder" && !l.hidden)
      .sort((a, b) => a.position - b.position);
    return responders.map((l) => {
      const m = messages.find(
        (x) => x.lane_id === l.id && x.turn_id === latestTurn.id && x.role === "assistant"
      );
      const tokens = m?.usage_json?.completion_tokens ?? null;
      const tokPerSec =
        tokens != null && m?.latency_ms
          ? tokens / (m.latency_ms / 1000)
          : null;
      return {
        laneId: l.id,
        model: l.model,
        chars: m?.content?.length ?? 0,
        tokPerSec,
        latencyMs: m?.latency_ms ?? null,
      };
    });
  }, [lanes, messages, latestTurn]);

  const fastest = rows.reduce<Row | null>(
    (best, r) => (r.tokPerSec != null && (!best || r.tokPerSec > (best.tokPerSec ?? 0)) ? r : best),
    null
  );
  const longest = rows.reduce<Row | null>(
    (best, r) => (r.chars > (best?.chars ?? -1) ? r : best),
    null
  );

  async function runSynthesis() {
    setBusy(true);
    setErr(null);
    try {
      const res = await apiFetch<{
        content: string;
        used_provider: string;
        used_model: string;
      }>(`/api/sessions/${sessionId}/synthesize`, {
        method: "POST",
        body: JSON.stringify({ turn_id: latestTurn?.id }),
      });
      setSynth({ content: res.content, provider: res.used_provider, model: res.used_model });
    } catch (e) {
      setErr((e as Error).message || "Synthesis failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="border-b border-gray-200 bg-gray-50 px-3 py-2 dark:border-gray-700 dark:bg-gray-900/40">
      <div className="mb-1 flex items-center justify-between">
        <span className="text-xs font-semibold uppercase tracking-wide text-gray-500">
          📊 Cross-lane insights
        </span>
        <div className="flex items-center gap-2">
          <button
            onClick={runSynthesis}
            disabled={busy || rows.length === 0}
            className="rounded bg-brand px-2 py-0.5 text-xs font-medium text-white hover:brightness-110 disabled:opacity-40"
            title="Merge all lane answers into one best answer using your default model"
          >
            {busy ? "Synthesizing…" : "✨ Synthesize best"}
          </button>
          <button
            onClick={onClose}
            className="rounded px-1 text-xs text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
            title="Close"
          >
            ✕
          </button>
        </div>
      </div>

      {rows.length === 0 ? (
        <p className="text-xs text-gray-400">Send a message to see comparative metrics.</p>
      ) : (
        <div className="flex flex-wrap gap-3 text-xs">
          <Stat label="⚡ Fastest" value={fastest ? `${fastest.model} · ${fastest.tokPerSec!.toFixed(1)} tok/s` : "—"} />
          <Stat label="📏 Most detailed" value={longest ? `${longest.model} · ${longest.chars} chars` : "—"} />
          <Stat label="Lanes" value={`${rows.length} responders`} />
        </div>
      )}

      {rows.length > 0 && (
        <div className="mt-1.5 overflow-x-auto">
          <table className="w-full border-collapse text-[11px]">
            <thead>
              <tr className="text-left text-gray-400">
                <th className="py-0.5 pr-3 font-medium">Model</th>
                <th className="py-0.5 pr-3 font-medium">tok/s</th>
                <th className="py-0.5 pr-3 font-medium">Latency</th>
                <th className="py-0.5 pr-3 font-medium">Length</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.laneId} className="text-gray-600 dark:text-gray-300">
                  <td className="py-0.5 pr-3">
                    {fastest?.laneId === r.laneId && "⚡ "}
                    {longest?.laneId === r.laneId && "📏 "}
                    {r.model}
                  </td>
                  <td className="py-0.5 pr-3 tabular-nums">
                    {r.tokPerSec != null ? r.tokPerSec.toFixed(1) : "—"}
                  </td>
                  <td className="py-0.5 pr-3 tabular-nums">
                    {r.latencyMs != null ? `${r.latencyMs} ms` : "—"}
                  </td>
                  <td className="py-0.5 pr-3 tabular-nums">{r.chars}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {err && <p className="mt-1 text-xs text-red-500">⚠️ {err}</p>}

      {synth && (
        <div className="mt-2 rounded-lg border border-brand/30 bg-white p-2 dark:bg-gray-900">
          <div className="mb-1 flex items-center justify-between">
            <span className="text-[10px] font-semibold uppercase tracking-wide text-brand">
              ✨ Synthesized answer · {synth.model}
            </span>
            <div className="flex items-center gap-1">
              <button
                onClick={() => navigator.clipboard.writeText(synth.content)}
                className="rounded px-1 text-[10px] text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
              >
                Copy
              </button>
              <button
                onClick={() => addArtifact(synth.content, "Synthesized answer")}
                className="rounded px-1 text-[10px] text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
              >
                📌 Pin
              </button>
            </div>
          </div>
          <div className="max-h-72 overflow-y-auto">
            <MessageRenderer content={synth.content} />
          </div>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded border border-gray-200 bg-white px-2 py-1 dark:border-gray-700 dark:bg-gray-800">
      <div className="text-[9px] uppercase tracking-wide text-gray-400">{label}</div>
      <div className="font-medium text-gray-700 dark:text-gray-200">{value}</div>
    </div>
  );
}
