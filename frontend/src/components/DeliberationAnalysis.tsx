import type { ConvergenceTrace, DeliberationParticipant, PanelMetrics } from "../api/deliberation";

function heat(value: number): string {
  // Low overlap (genuine disagreement) reads cool; high overlap reads warm. A panel that
  // is entirely warm by round 2 is a herding signal, not a success signal.
  if (value >= 0.8) return "bg-emerald-500";
  if (value >= 0.6) return "bg-emerald-400";
  if (value >= 0.4) return "bg-amber-400";
  if (value >= 0.2) return "bg-orange-400";
  return "bg-rose-400";
}

function short(model: string): string {
  return model.length > 14 ? `${model.slice(0, 13)}…` : model;
}

/**
 * The analysis drawer: how much the panel really agreed, who moved the needle, and who
 * simply folded. The capitulation column is the point — it makes sycophancy visible
 * instead of hiding it behind a green "converged" badge.
 */
export function DeliberationAnalysis({
  traces,
  metrics,
  participants,
  totalCalls,
  wallMs,
  onClose,
}: {
  traces: ConvergenceTrace[];
  metrics: PanelMetrics | null;
  participants: DeliberationParticipant[];
  totalCalls: number;
  wallMs: number;
  onClose: () => void;
}) {
  const nameOf = (laneId: string) =>
    participants.find((p) => p.lane_id === laneId)?.model ?? laneId.slice(0, 6);
  const last = traces[traces.length - 1];

  const influence = Object.entries(metrics?.influence ?? {}).sort((a, b) => b[1] - a[1]);
  const capitulation = Object.entries(metrics?.capitulation ?? {}).sort((a, b) => b[1] - a[1]);

  return (
    <aside className="flex w-80 shrink-0 flex-col border-l border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-900">
      <div className="flex items-center justify-between border-b border-gray-200 px-3 py-2 dark:border-gray-700">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500">
          Panel analysis
        </h3>
        <button
          onClick={onClose}
          className="rounded px-1.5 text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800"
        >
          ✕
        </button>
      </div>

      <div className="flex-1 space-y-5 overflow-y-auto p-3 text-xs">
        {last && (
          <section>
            <div className="mb-1 flex items-baseline justify-between">
              <h4 className="font-semibold text-gray-700 dark:text-gray-200">
                Claim overlap
              </h4>
              <span className="text-[10px] text-gray-400">
                diversity {last.diversity.toFixed(2)}
              </span>
            </div>
            <div className="overflow-x-auto">
              <table className="border-collapse text-[10px]">
                <thead>
                  <tr>
                    <th />
                    {last.labels.map((l) => (
                      <th key={l} className="px-1 pb-1 font-normal text-gray-400">
                        {short(nameOf(l)).slice(0, 6)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {last.labels.map((row, i) => (
                    <tr key={row}>
                      <td className="pr-1 text-right text-gray-400">{short(nameOf(row))}</td>
                      {last.labels.map((col, j) => (
                        <td key={col} className="p-0.5">
                          <div
                            title={`${nameOf(row)} ↔ ${nameOf(col)}: ${(
                              last.matrix[i][j] * 100
                            ).toFixed(0)}%`}
                            className={`h-6 w-6 rounded ${
                              i === j ? "bg-gray-200 dark:bg-gray-700" : heat(last.matrix[i][j])
                            }`}
                          />
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {last.diversity > 0.5 && (
              <p className="mt-1 text-[10px] text-gray-400">
                Lexical only — models can make the same point in different words, so read
                this relatively rather than as a percentage of agreement.
              </p>
            )}
          </section>
        )}

        {traces.length > 0 && (
          <section>
            <h4 className="mb-1 font-semibold text-gray-700 dark:text-gray-200">
              Approval by round
            </h4>
            <div className="flex h-20 items-end gap-2 rounded border border-gray-200 p-2 dark:border-gray-700">
              {traces.map((t) => (
                <div key={t.round} className="flex h-full flex-1 flex-col justify-end">
                  <div
                    className="rounded-t bg-indigo-500"
                    style={{ height: `${Math.max(4, t.agreement * 100)}%` }}
                    title={`round ${t.round}: ${t.approvals.length}/${t.responded.length} approved`}
                  />
                  <span className="mt-0.5 text-center text-[9px] text-gray-400">R{t.round}</span>
                </div>
              ))}
            </div>
          </section>
        )}

        {influence.length > 0 && (
          <section>
            <h4 className="mb-1 font-semibold text-gray-700 dark:text-gray-200">
              Influence
              <span className="ml-1 font-normal text-gray-400">— whose claims survived</span>
            </h4>
            <div className="space-y-1">
              {influence.map(([laneId, value]) => (
                <div key={laneId} className="flex items-center gap-2">
                  <span className="w-24 truncate text-gray-600 dark:text-gray-300">
                    {short(nameOf(laneId))}
                  </span>
                  <div className="h-2 flex-1 rounded bg-gray-100 dark:bg-gray-800">
                    <div
                      className="h-2 rounded bg-indigo-500"
                      style={{ width: `${Math.round(value * 100)}%` }}
                    />
                  </div>
                  <span className="w-9 text-right text-gray-500">
                    {Math.round(value * 100)}%
                  </span>
                </div>
              ))}
            </div>
          </section>
        )}

        {capitulation.length > 0 && (
          <section>
            <h4 className="mb-1 font-semibold text-gray-700 dark:text-gray-200">
              Capitulation
              <span className="ml-1 font-normal text-gray-400">— changed with no reason</span>
            </h4>
            <div className="space-y-1">
              {capitulation.map(([laneId, value]) => (
                <div key={laneId} className="flex items-center gap-2">
                  <span className="w-24 truncate text-gray-600 dark:text-gray-300">
                    {short(nameOf(laneId))}
                  </span>
                  <div className="h-2 flex-1 rounded bg-gray-100 dark:bg-gray-800">
                    <div
                      className={`h-2 rounded ${value > 0.4 ? "bg-rose-500" : "bg-emerald-500"}`}
                      style={{ width: `${Math.max(3, Math.round(value * 100))}%` }}
                    />
                  </div>
                  <span className="w-9 text-right text-gray-500">{value.toFixed(2)}</span>
                </div>
              ))}
            </div>
            <p className="mt-1 text-[10px] text-gray-400">Lower is better.</p>
          </section>
        )}

        <section className="border-t border-gray-200 pt-2 text-[10px] text-gray-500 dark:border-gray-700">
          {totalCalls} model calls · {(wallMs / 1000).toFixed(0)}s wall clock
          {" · "}
          <a
            href="/analytics"
            className="text-brand hover:underline"
            title="Which models earn their seat, across every deliberation"
          >
            Council leaderboard →
          </a>
        </section>
      </div>
    </aside>
  );
}
