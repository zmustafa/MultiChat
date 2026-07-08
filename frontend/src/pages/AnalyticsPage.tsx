import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { apiFetch } from "../api/client";
import { ThemeToggle } from "../components/ThemeToggle";
import { useAuth } from "../auth/AuthContext";

interface Agg {
  model?: string;
  provider?: string;
  date?: string;
  label?: string;
  responses: number;
  errors: number;
  error_rate: number;
  completion_tokens: number;
  prompt_tokens: number;
  tool_calls?: number;
  cost?: number;
  avg_latency_ms: number | null;
  avg_tok_per_sec: number | null;
}

interface Kpis {
  messages: number;
  responses: number;
  tool_calls: number;
  chats: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  estimated_cost: number;
}

interface Tools {
  total: number;
  by_status: { label: string; count: number }[];
  by_kind: { label: string; count: number }[];
  top: { name: string; count: number }[];
  success_rate: number | null;
  succeeded: number;
  completed: number;
}

interface UsageResponse {
  models: Agg[];
  providers: Agg[];
  daily: Agg[];
  totals: Agg;
  range_days: number;
  kpis: Kpis;
  tools: Tools;
  tokens: {
    prompt: number;
    completion: number;
    total: number;
    requests: number;
    estimated_cost: number;
  };
  cost_by_model: { model: string; cost: number; tokens: number }[];
  activity_24h: { label: string; messages: number; tool_calls: number }[];
  punchcard: { weekday: number; hour: number; count: number }[];
  active_chats: { id: string; title: string; events: number; updated_at: string | null }[];
}

const PALETTE = [
  "#6366F1",
  "#22C55E",
  "#38BDF8",
  "#F59E0B",
  "#EC4899",
  "#14B8A6",
  "#A855F7",
  "#EF4444",
];

function fmt(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return `${n}`;
}

function Card({
  title,
  right,
  children,
  className = "",
}: {
  title?: string;
  right?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`rounded-xl border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-gray-900 ${className}`}
    >
      {title && (
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-200">{title}</h3>
          {right}
        </div>
      )}
      {children}
    </div>
  );
}

/** Multi-segment donut chart (inline SVG — no chart lib). */
function Donut({
  segments,
  center,
  sub,
  size = 96,
}: {
  segments: { label: string; value: number; color: string }[];
  center: string;
  sub?: string;
  size?: number;
}) {
  const stroke = 12;
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const total = segments.reduce((s, x) => s + x.value, 0) || 1;
  let offset = 0;
  return (
    <div className="flex items-center gap-4">
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="shrink-0">
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke="currentColor"
          className="text-gray-100 dark:text-gray-800"
          strokeWidth={stroke}
        />
        {segments.map((s, i) => {
          const len = (s.value / total) * c;
          const el = (
            <circle
              key={i}
              cx={size / 2}
              cy={size / 2}
              r={r}
              fill="none"
              stroke={s.color}
              strokeWidth={stroke}
              strokeDasharray={`${len} ${c - len}`}
              strokeDashoffset={-offset}
              transform={`rotate(-90 ${size / 2} ${size / 2})`}
            />
          );
          offset += len;
          return el;
        })}
        <text
          x="50%"
          y="47%"
          textAnchor="middle"
          dominantBaseline="middle"
          className="fill-gray-800 dark:fill-gray-100"
          style={{ fontSize: 18, fontWeight: 700 }}
        >
          {center}
        </text>
        {sub && (
          <text
            x="50%"
            y="63%"
            textAnchor="middle"
            dominantBaseline="middle"
            className="fill-gray-400"
            style={{ fontSize: 8 }}
          >
            {sub}
          </text>
        )}
      </svg>
      <div className="min-w-0 flex-1 space-y-1">
        {segments.map((s, i) => (
          <div key={i} className="flex items-center justify-between gap-2 text-xs">
            <span className="flex min-w-0 items-center gap-1.5">
              <span className="h-2 w-2 shrink-0 rounded-sm" style={{ background: s.color }} />
              <span className="truncate text-gray-600 dark:text-gray-300">{s.label}</span>
            </span>
            <span className="tabular-nums text-gray-400">{fmt(s.value)}</span>
          </div>
        ))}
        {segments.length === 0 && <span className="text-xs text-gray-400">No data</span>}
      </div>
    </div>
  );
}

function BarList({
  rows,
  unit = "",
  color = "#38BDF8",
}: {
  rows: { label: string; value: number }[];
  unit?: string;
  color?: string;
}) {
  const max = Math.max(1, ...rows.map((r) => r.value));
  if (rows.length === 0)
    return <div className="text-xs text-gray-400">No data in this range.</div>;
  return (
    <div className="space-y-1.5">
      {rows.map((r, i) => (
        <div key={i} className="flex items-center gap-2 text-xs">
          <span
            className="w-28 shrink-0 truncate font-mono text-gray-600 dark:text-gray-300"
            title={r.label}
          >
            {r.label}
          </span>
          <div className="h-3 flex-1 overflow-hidden rounded bg-gray-100 dark:bg-gray-800">
            <div
              className="h-full rounded"
              style={{ width: `${Math.max(3, (r.value / max) * 100)}%`, background: color }}
            />
          </div>
          <span className="w-12 shrink-0 text-right tabular-nums text-gray-500">
            {r.value}
            {unit}
          </span>
        </div>
      ))}
    </div>
  );
}

function Kpi({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-gray-900">
      <div className="text-2xl font-bold tabular-nums text-gray-800 dark:text-gray-100">
        {value}
      </div>
      <div className="mt-0.5 text-xs font-medium text-gray-500">{label}</div>
      {sub && <div className="mt-0.5 text-[11px] text-gray-400">{sub}</div>}
    </div>
  );
}

const RANGES: { label: string; days: number }[] = [
  { label: "1d", days: 1 },
  { label: "3d", days: 3 },
  { label: "7d", days: 7 },
  { label: "2w", days: 14 },
  { label: "30d", days: 30 },
  { label: "All", days: 0 },
];

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export function AnalyticsPage() {
  const { logout, user } = useAuth();
  const [days, setDays] = useState(7);
  const { data, isLoading, refetch, isFetching } = useQuery({
    queryKey: ["analytics-usage", days],
    queryFn: () => apiFetch<UsageResponse>(`/api/analytics/usage?days=${days}`),
  });

  const k = data?.kpis;
  const tools = data?.tools;
  const tokens = data?.tokens;
  const daily7 = (data?.daily ?? []).slice(-7);
  const maxDaily = Math.max(
    1,
    ...daily7.map((d) => (d.responses || 0) + (d.tool_calls || 0)),
  );
  const max24 = Math.max(
    1,
    ...(data?.activity_24h ?? []).map((h) => h.messages + h.tool_calls),
  );
  const punchMap = new Map<string, number>();
  let punchMax = 1;
  for (const p of data?.punchcard ?? []) {
    punchMap.set(`${p.weekday}:${p.hour}`, p.count);
    punchMax = Math.max(punchMax, p.count);
  }

  const providerSegs = (data?.providers ?? []).slice(0, 8).map((p, i) => ({
    label: p.provider || "?",
    value: p.responses,
    color: PALETTE[i % PALETTE.length],
  }));
  const statusColors: Record<string, string> = {
    ok: "#22C55E",
    done: "#22C55E",
    running: "#38BDF8",
    error: "#EF4444",
    failed: "#EF4444",
  };
  const statusSegs = (tools?.by_status ?? []).map((s, i) => ({
    label: s.label,
    value: s.count,
    color: statusColors[s.label] || PALETTE[i % PALETTE.length],
  }));
  const kindSegs = (tools?.by_kind ?? []).map((s) => ({
    label: s.label,
    value: s.count,
    color: s.label === "Write" ? "#F59E0B" : "#38BDF8",
  }));
  const tokenSegs = [
    { label: "Prompt", value: tokens?.prompt ?? 0, color: "#6366F1" },
    { label: "Completion", value: tokens?.completion ?? 0, color: "#38BDF8" },
  ];

  return (
    <div className="flex h-full flex-col bg-gray-50 dark:bg-gray-950">
      <header className="flex items-center justify-between border-b border-gray-200 bg-white px-4 py-2 dark:border-gray-700 dark:bg-gray-900">
        <div className="flex items-center gap-3">
          <Link to="/" className="text-sm text-blue-500">
            ← Chat
          </Link>
          <h1 className="text-lg font-semibold">📊 Insights</h1>
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

      <div className="flex-1 overflow-y-auto p-4">
        <div className="mx-auto max-w-7xl space-y-4">
          <div className="flex items-center justify-between gap-2">
            <p className="hidden text-xs text-gray-500 sm:block">
              At-a-glance usage &amp; health — messages, tool calls, token usage &amp; cost,
              provider mix and activity trends.
            </p>
            <div className="flex items-center gap-2">
              <div className="flex overflow-hidden rounded-lg border border-gray-200 text-xs dark:border-gray-700">
                {RANGES.map((r) => (
                  <button
                    key={r.label}
                    onClick={() => setDays(r.days)}
                    className={`px-2.5 py-1 ${
                      days === r.days
                        ? "bg-brand text-white"
                        : "bg-white text-gray-500 hover:bg-gray-50 dark:bg-gray-900 dark:hover:bg-gray-800"
                    }`}
                  >
                    {r.label}
                  </button>
                ))}
              </div>
              <button
                onClick={() => refetch()}
                title="Refresh"
                className="rounded-lg border border-gray-200 px-2 py-1 text-xs text-gray-500 hover:bg-gray-50 dark:border-gray-700 dark:hover:bg-gray-800"
              >
                {isFetching ? "…" : "↻"}
              </button>
            </div>
          </div>

          {isLoading || !data ? (
            <p className="text-sm text-gray-500">Loading…</p>
          ) : (
            <>
              {/* KPI cards */}
              <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
                <Kpi label="Messages" value={fmt(k?.messages ?? 0)} sub="user prompts" />
                <Kpi label="Responses" value={fmt(k?.responses ?? 0)} sub="model answers" />
                <Kpi label="Tool calls" value={fmt(k?.tool_calls ?? 0)} />
                <Kpi label="Chats" value={fmt(k?.chats ?? 0)} sub="active in range" />
                <Kpi label="Total tokens" value={fmt(k?.total_tokens ?? 0)} />
                <Kpi
                  label="Est. cost"
                  value={`$${(k?.estimated_cost ?? 0).toFixed(2)}`}
                  sub="estimate"
                />
              </div>

              {/* Token usage / provider mix / tool status */}
              <div className="grid grid-cols-1 gap-3 lg:grid-cols-4">
                <Card title="Token usage" className="lg:col-span-2">
                  <div className="mb-1 flex items-end justify-between">
                    <span className="text-2xl font-bold tabular-nums">
                      {fmt(tokens?.total ?? 0)}
                    </span>
                    <span className="text-xs text-gray-400">
                      {tokens?.requests ?? 0} requests
                    </span>
                  </div>
                  <div className="mb-2 flex h-2.5 overflow-hidden rounded bg-gray-100 dark:bg-gray-800">
                    <div
                      className="h-full bg-indigo-500"
                      style={{
                        width: `${((tokens?.prompt ?? 0) / Math.max(1, tokens?.total ?? 1)) * 100}%`,
                      }}
                    />
                    <div
                      className="h-full bg-sky-400"
                      style={{
                        width: `${((tokens?.completion ?? 0) / Math.max(1, tokens?.total ?? 1)) * 100}%`,
                      }}
                    />
                  </div>
                  <div className="mb-3 flex justify-between text-[11px] text-gray-500">
                    <span>Prompt {fmt(tokens?.prompt ?? 0)}</span>
                    <span>Completion {fmt(tokens?.completion ?? 0)}</span>
                  </div>
                  <div className="mb-1 flex items-center justify-between text-xs">
                    <span className="text-gray-500">Estimated cost</span>
                    <span className="font-semibold text-green-600">
                      ${(tokens?.estimated_cost ?? 0).toFixed(2)}
                    </span>
                  </div>
                  <BarList
                    rows={(data.cost_by_model ?? []).map((cm) => ({
                      label: cm.model,
                      value: Number(cm.cost.toFixed(2)),
                    }))}
                    color="#22C55E"
                  />
                </Card>

                <Card title="Provider mix">
                  <Donut segments={providerSegs} center={fmt(k?.responses ?? 0)} sub="responses" />
                </Card>

                <Card title="Tool calls by status">
                  <Donut segments={statusSegs} center={fmt(tools?.total ?? 0)} sub="calls" />
                </Card>
              </div>

              {/* Activity charts */}
              <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
                <Card title="Activity — last 7 days">
                  <div className="flex h-40 items-end gap-2">
                    {daily7.length === 0 && (
                      <span className="text-xs text-gray-400">No activity.</span>
                    )}
                    {daily7.map((d, i) => {
                      const total = (d.responses || 0) + (d.tool_calls || 0);
                      return (
                        <div key={i} className="flex h-full flex-1 flex-col items-center justify-end gap-1">
                          <div
                            className="flex w-full flex-col justify-end rounded-t"
                            style={{ height: `${(total / maxDaily) * 100}%` }}
                            title={`${d.date}: ${d.responses} responses, ${d.tool_calls} tools`}
                          >
                            <div
                              className="w-full bg-sky-400"
                              style={{
                                height: `${((d.tool_calls || 0) / Math.max(1, total)) * 100}%`,
                              }}
                            />
                            <div
                              className="w-full rounded-t bg-blue-600"
                              style={{
                                height: `${((d.responses || 0) / Math.max(1, total)) * 100}%`,
                              }}
                            />
                          </div>
                          <span className="text-[9px] text-gray-400">
                            {(d.date || "").slice(5)}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                  <div className="mt-2 flex gap-3 text-[11px] text-gray-500">
                    <span className="flex items-center gap-1">
                      <span className="h-2 w-2 rounded-sm bg-blue-600" /> Responses
                    </span>
                    <span className="flex items-center gap-1">
                      <span className="h-2 w-2 rounded-sm bg-sky-400" /> Tool calls
                    </span>
                  </div>
                </Card>

                <Card title="Activity — last 24 hours">
                  <div className="flex h-40 items-end gap-0.5">
                    {(data.activity_24h ?? []).map((h, i) => {
                      const total = h.messages + h.tool_calls;
                      return (
                        <div
                          key={i}
                          className="flex h-full flex-1 flex-col justify-end"
                          title={`${h.label}: ${h.messages} msg, ${h.tool_calls} tools`}
                        >
                          <div
                            className="w-full rounded-t bg-indigo-400"
                            style={{ height: `${(total / max24) * 100}%` }}
                          />
                        </div>
                      );
                    })}
                  </div>
                  <div className="mt-2 flex justify-between text-[9px] text-gray-400">
                    <span>{data.activity_24h?.[0]?.label}</span>
                    <span>{data.activity_24h?.[data.activity_24h.length - 1]?.label}</span>
                  </div>
                </Card>
              </div>

              {/* Punch card */}
              <Card title="Activity punch-card — busiest weekday × hour">
                <div className="overflow-x-auto">
                  <div className="min-w-[640px]">
                    <div className="flex">
                      <div className="w-8 shrink-0" />
                      {Array.from({ length: 24 }).map((_, h) => (
                        <div key={h} className="flex-1 text-center text-[8px] text-gray-400">
                          {h % 3 === 0 ? h : ""}
                        </div>
                      ))}
                    </div>
                    {WEEKDAYS.map((wd, w) => (
                      <div key={wd} className="flex items-center">
                        <div className="w-8 shrink-0 text-[9px] text-gray-400">{wd}</div>
                        {Array.from({ length: 24 }).map((_, h) => {
                          const count = punchMap.get(`${w}:${h}`) || 0;
                          const intensity = count / punchMax;
                          return (
                            <div key={h} className="flex-1 p-0.5">
                              <div
                                className="aspect-square w-full rounded-sm"
                                style={{
                                  background:
                                    count === 0
                                      ? "rgba(148,163,184,0.12)"
                                      : `rgba(99,102,241,${0.2 + intensity * 0.8})`,
                                }}
                                title={`${wd} ${h}:00 — ${count} events`}
                              />
                            </div>
                          );
                        })}
                      </div>
                    ))}
                  </div>
                </div>
              </Card>

              {/* Breakdowns */}
              <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
                <Card title="Prompt vs completion tokens">
                  <Donut segments={tokenSegs} center={fmt(tokens?.total ?? 0)} sub="tokens" />
                </Card>
                <Card title="Tool calls by kind">
                  <Donut segments={kindSegs} center={fmt(tools?.total ?? 0)} sub="calls" />
                </Card>
                <Card title="Tool success rate">
                  <div className="flex flex-col items-center justify-center py-2">
                    <Donut
                      segments={
                        tools?.completed
                          ? [
                              { label: "Succeeded", value: tools.succeeded, color: "#22C55E" },
                              {
                                label: "Failed",
                                value: tools.completed - tools.succeeded,
                                color: "#EF4444",
                              },
                            ]
                          : []
                      }
                      center={
                        tools?.success_rate != null
                          ? `${Math.round(tools.success_rate * 100)}%`
                          : "—"
                      }
                      sub={`${tools?.completed ?? 0} done`}
                    />
                  </div>
                </Card>
                <Card title="Estimated cost by model">
                  <BarList
                    rows={(data.cost_by_model ?? []).map((cm) => ({
                      label: cm.model,
                      value: Number(cm.cost.toFixed(2)),
                    }))}
                    color="#22C55E"
                  />
                  <p className="mt-2 text-[10px] text-gray-400">
                    Estimated from token counts &amp; standard per-model rates — for visibility, not
                    billing.
                  </p>
                </Card>
              </div>

              {/* Top tools + most active chats */}
              <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
                <Card
                  title="Top tools"
                  right={
                    <span className="rounded-full bg-green-100 px-2 py-0.5 text-[10px] font-medium text-green-700 dark:bg-green-900/40 dark:text-green-300">
                      {tools?.total ?? 0} calls
                    </span>
                  }
                >
                  <BarList
                    rows={(tools?.top ?? []).map((t) => ({ label: t.name, value: t.count }))}
                    color="#38BDF8"
                  />
                </Card>
                <Card
                  title="Most active chats"
                  right={
                    <Link to="/" className="text-xs text-blue-500">
                      All chats →
                    </Link>
                  }
                >
                  {(data.active_chats ?? []).length === 0 ? (
                    <div className="text-xs text-gray-400">No activity in this range.</div>
                  ) : (
                    <div className="space-y-2">
                      {data.active_chats.map((chat) => {
                        const max = Math.max(1, ...data.active_chats.map((x) => x.events));
                        return (
                          <Link key={chat.id} to={`/c/${chat.id}`} className="block" title={chat.title}>
                            <div className="mb-0.5 flex items-center justify-between gap-2 text-xs">
                              <span className="min-w-0 flex-1 truncate font-medium text-gray-700 hover:text-brand dark:text-gray-200">
                                {chat.title}
                              </span>
                              <span className="shrink-0 tabular-nums text-gray-400">
                                {chat.events} events
                              </span>
                            </div>
                            <div className="h-1.5 overflow-hidden rounded bg-gray-100 dark:bg-gray-800">
                              <div
                                className="h-full rounded bg-blue-500"
                                style={{ width: `${(chat.events / max) * 100}%` }}
                              />
                            </div>
                          </Link>
                        );
                      })}
                    </div>
                  )}
                </Card>
              </div>

              {/* Detailed tables */}
              <section>
                <h2 className="mb-2 text-sm font-medium text-gray-500">By model</h2>
                <Table rows={data.models ?? []} keyLabel="Model" />
              </section>
              <section>
                <h2 className="mb-2 text-sm font-medium text-gray-500">By provider</h2>
                <Table rows={data.providers ?? []} keyLabel="Provider" />
              </section>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function Bar({ value, max }: { value: number; max: number }) {
  const pct = max > 0 ? Math.max(2, (value / max) * 100) : 0;
  return (
    <div className="h-1.5 w-24 overflow-hidden rounded bg-gray-200 dark:bg-gray-700">
      <div className="h-full rounded bg-brand" style={{ width: `${pct}%` }} />
    </div>
  );
}

function Table({ rows, keyLabel }: { rows: Agg[]; keyLabel: string }) {
  const maxTokens = Math.max(1, ...rows.map((r) => r.completion_tokens));
  return (
    <div className="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-700">
      <table className="w-full text-sm">
        <thead className="bg-gray-50 text-left text-xs text-gray-500 dark:bg-gray-800/50">
          <tr>
            <th className="px-3 py-2 font-medium">{keyLabel}</th>
            <th className="px-3 py-2 font-medium">Responses</th>
            <th className="px-3 py-2 font-medium">Completion tokens</th>
            <th className="px-3 py-2 font-medium">Avg latency</th>
            <th className="px-3 py-2 font-medium">Avg tok/s</th>
            <th className="px-3 py-2 font-medium">Error rate</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr
              key={r.model || r.provider || r.date}
              className="border-t border-gray-100 dark:border-gray-800"
            >
              <td className="px-3 py-2 font-medium">{r.model || r.provider || r.date}</td>
              <td className="px-3 py-2 tabular-nums">{r.responses}</td>
              <td className="px-3 py-2">
                <div className="flex items-center gap-2">
                  <span className="tabular-nums">{r.completion_tokens}</span>
                  <Bar value={r.completion_tokens} max={maxTokens} />
                </div>
              </td>
              <td className="px-3 py-2 tabular-nums">
                {r.avg_latency_ms != null ? `${r.avg_latency_ms} ms` : "—"}
              </td>
              <td className="px-3 py-2 tabular-nums">
                {r.avg_tok_per_sec != null ? r.avg_tok_per_sec.toFixed(1) : "—"}
              </td>
              <td className="px-3 py-2 tabular-nums">
                <span className={r.error_rate > 0 ? "text-red-500" : ""}>
                  {(r.error_rate * 100).toFixed(0)}%
                </span>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td colSpan={6} className="px-3 py-6 text-center text-gray-400">
                No usage data yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
