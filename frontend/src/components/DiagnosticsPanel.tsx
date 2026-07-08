export interface DiagStep {
  step: string;
  status: "ok" | "error" | "warn" | "running" | string;
  title: string;
  detail?: string;
}

const TEST_ORDER: { id: string; label: string }[] = [
  { id: "config", label: "Load configuration" },
  { id: "endpoint", label: "Resolve endpoint (DNS)" },
  { id: "connect", label: "Connect (TCP / TLS)" },
  { id: "auth", label: "Authenticate" },
  { id: "request", label: "Send probe request" },
  { id: "first_token", label: "Receive first token" },
  { id: "complete", label: "Complete" },
];

const MODELS_ORDER: { id: string; label: string }[] = [
  { id: "config", label: "Load configuration" },
  { id: "endpoint", label: "Resolve endpoint (DNS)" },
  { id: "connect", label: "Connect (TCP / TLS)" },
  { id: "fetch", label: "Fetch model catalogue" },
  { id: "complete", label: "Complete" },
];

export function DiagnosticsPanel({
  running,
  steps,
  result,
  title,
  variant = "test",
}: {
  running: boolean;
  steps: DiagStep[];
  result?: { ok: boolean; detail: string };
  title?: string;
  variant?: "test" | "models";
}) {
  const order = variant === "models" ? MODELS_ORDER : TEST_ORDER;
  const byId: Record<string, DiagStep | undefined> = {};
  for (const s of steps) byId[s.step] = s;

  let runningIdx = -1;
  if (running) runningIdx = order.findIndex((o) => !byId[o.id]);

  return (
    <div className="mt-3 rounded-lg border border-gray-200 bg-gray-50/70 p-3 dark:border-gray-700 dark:bg-gray-800/50">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">
          {title ?? "Connection diagnostics"}
        </div>
        {result && (
          <div
            className={`truncate text-xs font-medium ${
              result.ok ? "text-green-600" : "text-red-600"
            }`}
            title={result.detail}
          >
            {result.ok ? "✓ Healthy" : "✗ Failed"} · {result.detail}
          </div>
        )}
      </div>
      <ol className="space-y-1">
        {order.map((o, i) => {
          const s = byId[o.id];
          const isRunning = i === runningIdx;
          const pending = !s && !isRunning;
          const icon = s
            ? s.status === "ok"
              ? "✓"
              : s.status === "error"
                ? "✗"
                : s.status === "warn"
                  ? "!"
                  : "–"
            : isRunning
              ? "◌"
              : "·";
          const iconClass = s
            ? s.status === "ok"
              ? "bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-300"
              : s.status === "error"
                ? "bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-300"
                : s.status === "warn"
                  ? "bg-amber-100 text-amber-700 dark:bg-amber-900/50 dark:text-amber-300"
                  : "bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-300"
            : isRunning
              ? "bg-blue-100 text-blue-700 animate-pulse dark:bg-blue-900/50 dark:text-blue-300"
              : "bg-gray-100 text-gray-400 dark:bg-gray-800 dark:text-gray-500";
          return (
            <li
              key={o.id}
              className={`flex items-start gap-2 rounded px-1.5 py-1 text-xs ${
                pending ? "opacity-50" : ""
              } ${s?.status === "error" ? "bg-red-50/60 dark:bg-red-950/30" : ""}`}
            >
              <span
                className={`mt-px inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full text-[10px] font-bold ${iconClass}`}
              >
                {icon}
              </span>
              <div className="min-w-0 flex-1">
                <span className="font-medium text-gray-700 dark:text-gray-200">
                  {s ? s.title : o.label}
                </span>
                {s?.detail && (
                  <span className="ml-1 truncate text-gray-400">— {s.detail}</span>
                )}
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
