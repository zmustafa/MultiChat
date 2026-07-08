import { useEffect, useMemo, useState } from "react";
import { apiFetch, asUtcDate } from "../api/client";
import { MessageRenderer } from "./MessageRenderer";

interface Snapshot {
  id: string;
  session_id: string | null;
  prompt: string;
  model: string;
  provider_name: string | null;
  content: string;
  label: string | null;
  created_at: string;
}

function groupKey(s: Snapshot): string {
  return `${s.model} · ${s.prompt.slice(0, 80)}`;
}

export function SnapshotsPanel({ onClose }: { onClose: () => void }) {
  const [snaps, setSnaps] = useState<Snapshot[]>([]);
  const [loading, setLoading] = useState(true);
  const [group, setGroup] = useState<string | null>(null);
  const [a, setA] = useState<string | null>(null);
  const [b, setB] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    try {
      setSnaps(await apiFetch<Snapshot[]>("/api/snapshots"));
    } catch {
      setSnaps([]);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  const groups = useMemo(() => {
    const m = new Map<string, Snapshot[]>();
    for (const s of snaps) {
      const k = groupKey(s);
      (m.get(k) || m.set(k, []).get(k)!).push(s);
    }
    return m;
  }, [snaps]);

  const current = group ? groups.get(group) || [] : [];
  const snapA = current.find((s) => s.id === a);
  const snapB = current.find((s) => s.id === b);

  async function remove(id: string) {
    await apiFetch(`/api/snapshots/${id}`, { method: "DELETE" }).catch(() => {});
    setSnaps((p) => p.filter((s) => s.id !== id));
  }

  return (
    <div className="border-b border-gray-200 bg-gray-50 px-3 py-2 dark:border-gray-700 dark:bg-gray-900/40">
      <div className="mb-1 flex items-center justify-between">
        <span className="text-xs font-semibold text-gray-600 dark:text-gray-300">
          📌 Pinned answers — compare across runs {snaps.length > 0 && `(${snaps.length})`}
        </span>
        <div className="flex gap-2">
          <button onClick={load} className="text-xs text-gray-400 hover:text-brand" title="Refresh">↻</button>
          <button onClick={onClose} className="text-xs text-gray-400 hover:text-red-500" title="Close">✕</button>
        </div>
      </div>
      {loading ? (
        <div className="py-2 text-xs text-gray-400">Loading…</div>
      ) : snaps.length === 0 ? (
        <div className="py-2 text-xs text-gray-400">
          No pinned answers yet. Click 📌 under any answer to pin it, then pin the same
          prompt+model again later to compare them here.
        </div>
      ) : (
        <div>
          <div className="mb-2 flex flex-wrap gap-1.5">
            {[...groups.entries()].map(([k, list]) => (
              <button
                key={k}
                onClick={() => {
                  setGroup(k);
                  setA(list[0]?.id ?? null);
                  setB(list[1]?.id ?? null);
                }}
                className={`max-w-[260px] truncate rounded-full border px-2 py-0.5 text-xs ${
                  group === k
                    ? "border-brand bg-brand/10 text-brand"
                    : "border-gray-300 bg-white text-gray-600 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300"
                }`}
                title={k}
              >
                {k} ({list.length})
              </button>
            ))}
          </div>
          {current.length > 0 && (
            <div className="grid grid-cols-2 gap-3">
              {[
                { sel: a, set: setA, snap: snapA, side: "A" },
                { sel: b, set: setB, snap: snapB, side: "B" },
              ].map(({ sel, set, snap, side }) => (
                <div key={side} className="min-w-0">
                  <select
                    value={sel ?? ""}
                    onChange={(e) => set(e.target.value || null)}
                    className="mb-1 w-full rounded border border-gray-300 px-2 py-1 text-xs dark:border-gray-600 dark:bg-gray-800"
                  >
                    <option value="">— select run —</option>
                    {current.map((s) => (
                      <option key={s.id} value={s.id}>
                        {asUtcDate(s.created_at).toLocaleString()}
                        {s.label ? ` · ${s.label}` : ""}
                      </option>
                    ))}
                  </select>
                  {snap ? (
                    <div className="max-h-72 overflow-y-auto rounded border border-gray-200 bg-white p-2 dark:border-gray-700 dark:bg-gray-900">
                      <div className="mb-1 flex items-center justify-between text-[10px] text-gray-400">
                        <span>{asUtcDate(snap.created_at).toLocaleString()}</span>
                        <button onClick={() => remove(snap.id)} className="hover:text-red-500" title="Delete snapshot">🗑</button>
                      </div>
                      <MessageRenderer content={snap.content} />
                    </div>
                  ) : (
                    <div className="rounded border border-dashed border-gray-300 p-3 text-center text-xs text-gray-400 dark:border-gray-600">
                      Select a run
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
