import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { apiFetch } from "../api/client";
import { ThemeToggle } from "../components/ThemeToggle";
import { ToolsSection } from "../components/ProviderSettings";
import { useAuth } from "../auth/AuthContext";

interface WorkIqTool {
  name: string;
  raw_name: string;
  description: string;
}
interface WorkIqStatus {
  enabled: boolean;
  connected: boolean;
  command: string;
  default_command: string;
  error: string | null;
  saved: boolean;
  eula_accepted: boolean;
  eula_url: string;
  tools: WorkIqTool[];
}

export function IntegrationsPage() {
  const { logout, user } = useAuth();
  const qc = useQueryClient();
  const [eulaOpen, setEulaOpen] = useState(false);
  const { data, isLoading } = useQuery({
    queryKey: ["integration-workiq"],
    queryFn: () => apiFetch<WorkIqStatus>("/api/integrations/workiq"),
  });
  const invalidate = () => qc.invalidateQueries({ queryKey: ["integration-workiq"] });

  const connect = useMutation({
    mutationFn: () =>
      apiFetch<WorkIqStatus>("/api/integrations/workiq/connect", {
        method: "POST",
        body: JSON.stringify({}),
      }),
    onSettled: invalidate,
  });
  const disconnect = useMutation({
    mutationFn: () =>
      apiFetch<WorkIqStatus>("/api/integrations/workiq/disconnect", {
        method: "POST",
      }),
    onSuccess: invalidate,
  });
  const acceptEula = useMutation({
    mutationFn: () =>
      apiFetch<{ result: string }>("/api/integrations/workiq/accept-eula", {
        method: "POST",
      }),
    onSuccess: () => {
      setEulaOpen(false);
      invalidate();
    },
  });
  const [testQuestion, setTestQuestion] = useState(
    "What are my upcoming meetings this week?"
  );
  const test = useMutation({
    mutationFn: (question: string) =>
      apiFetch<{ ok: boolean; result: string }>("/api/integrations/workiq/test", {
        method: "POST",
        body: JSON.stringify({ question }),
      }),
  });

  const connected = data?.connected;
  const eulaAccepted = data?.eula_accepted;
  const eulaUrl = data?.eula_url || "https://github.com/microsoft/work-iq";

  return (
    <div className="flex h-full flex-col bg-gray-50 dark:bg-gray-950">
      <header className="flex items-center justify-between border-b border-gray-200 bg-white px-4 py-2 dark:border-gray-700 dark:bg-gray-900">
        <div className="flex items-center gap-3">
          <Link to="/" className="text-sm text-blue-500">
            ← Chat
          </Link>
          <h1 className="text-lg font-semibold">Integrations</h1>
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
        <div className="mx-auto max-w-3xl space-y-4">
          <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm dark:border-gray-700 dark:bg-gray-900">
            <div className="flex items-start justify-between gap-3">
              <div>
                <h2 className="flex items-center gap-2 text-base font-semibold">
                  🔗 Microsoft Work IQ
                  {connected ? (
                    <span className="rounded-full bg-green-100 px-2 py-0.5 text-[10px] font-medium text-green-700 dark:bg-green-900/40 dark:text-green-300">
                      ● connected
                    </span>
                  ) : (
                    <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-medium text-gray-500 dark:bg-gray-800">
                      not connected
                    </span>
                  )}
                </h2>
                <p className="mt-1 max-w-xl text-xs text-gray-500">
                  Connect the Work IQ MCP server to let every chat's models query your
                  Microsoft 365 data — emails, meetings, documents, Teams, and people —
                  with natural language. Tools become available in any chat that has
                  Tools enabled.
                </p>
              </div>
              <div className="flex shrink-0 gap-2">
                {connected ? (
                  <button
                    onClick={() => disconnect.mutate()}
                    disabled={disconnect.isPending}
                    className="rounded border border-gray-300 px-3 py-1.5 text-sm hover:bg-gray-50 disabled:opacity-40 dark:border-gray-600 dark:hover:bg-gray-800"
                  >
                    {disconnect.isPending ? "Disconnecting…" : "Disconnect"}
                  </button>
                ) : (
                  <button
                    onClick={() => connect.mutate()}
                    disabled={connect.isPending}
                    className="rounded bg-brand px-3 py-1.5 text-sm font-medium text-white hover:brightness-110 disabled:opacity-40"
                  >
                    {connect.isPending ? "Connecting…" : "Connect"}
                  </button>
                )}
              </div>
            </div>

            <div className="mt-3 rounded-lg border border-gray-100 bg-gray-50 p-3 text-[11px] text-gray-500 dark:border-gray-800 dark:bg-gray-800/40">
              <p className="mb-1 font-medium text-gray-600 dark:text-gray-300">
                Prerequisites
              </p>
              <ul className="list-disc space-y-0.5 pl-4">
                <li>Node.js 18+ on the server (Work IQ runs via <code>npx</code>).</li>
                <li>
                  Microsoft 365 tenant with Entra consent — a consent/device-code prompt
                  may appear the first time a tool is used.
                </li>
                <li>
                  Launch command:{" "}
                  <code className="rounded bg-white px-1 dark:bg-gray-900">
                    {data?.default_command || "npx -y @microsoft/workiq@latest mcp"}
                  </code>
                </li>
              </ul>
            </div>

            {connected && (
              <div className="mt-3 flex items-center justify-between rounded-lg border border-gray-200 bg-white px-3 py-2 dark:border-gray-700 dark:bg-gray-900">
                <div className="text-xs">
                  <span className="font-medium text-gray-600 dark:text-gray-300">
                    End User License Agreement
                  </span>
                  <span className="ml-2">
                    {eulaAccepted ? (
                      <span className="rounded-full bg-green-100 px-2 py-0.5 text-[10px] font-medium text-green-700 dark:bg-green-900/40 dark:text-green-300">
                        ✓ accepted
                      </span>
                    ) : (
                      <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-medium text-amber-700 dark:bg-amber-900/40 dark:text-amber-300">
                        required
                      </span>
                    )}
                  </span>
                  <p className="mt-0.5 text-[11px] text-gray-400">
                    Accept the EULA to enable full access to Work IQ tools — you can do
                    this here, before chatting.
                  </p>
                </div>
                {!eulaAccepted && (
                  <button
                    onClick={() => setEulaOpen(true)}
                    className="shrink-0 rounded bg-brand px-3 py-1.5 text-sm font-medium text-white hover:brightness-110"
                  >
                    Accept EULA
                  </button>
                )}
              </div>
            )}

            {connected && (
              <div className="mt-3 rounded-lg border border-gray-200 bg-white p-3 dark:border-gray-700 dark:bg-gray-900">
                <p className="mb-1 text-xs font-medium text-gray-600 dark:text-gray-300">
                  Test connection
                </p>
                <p className="mb-2 text-[11px] text-gray-400">
                  Ask Microsoft 365 a question to confirm Work IQ is working end-to-end.
                  {!eulaAccepted && " Accept the EULA first for full access."}
                </p>
                <div className="flex gap-2">
                  <input
                    value={testQuestion}
                    onChange={(e) => setTestQuestion(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !test.isPending)
                        test.mutate(testQuestion);
                    }}
                    placeholder="What are my upcoming meetings this week?"
                    className="flex-1 rounded border border-gray-300 px-2 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800"
                  />
                  <button
                    onClick={() => test.mutate(testQuestion)}
                    disabled={test.isPending || !testQuestion.trim()}
                    className="shrink-0 rounded bg-brand px-3 py-1.5 text-sm font-medium text-white hover:brightness-110 disabled:opacity-40"
                  >
                    {test.isPending ? "Running…" : "▶ Run test"}
                  </button>
                </div>
                {test.isError && (
                  <p className="mt-2 rounded bg-red-50 px-2 py-1.5 text-xs text-red-600 dark:bg-red-950/40 dark:text-red-300">
                    ⚠️ {(test.error as Error).message}
                  </p>
                )}
                {test.data && (
                  <div
                    className={`mt-2 rounded-lg border p-2 text-xs ${
                      test.data.ok
                        ? "border-green-200 bg-green-50 dark:border-green-900/50 dark:bg-green-950/20"
                        : "border-red-200 bg-red-50 dark:border-red-900/50 dark:bg-red-950/20"
                    }`}
                  >
                    <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-gray-400">
                      {test.data.ok ? "✓ Response" : "✕ Failed"}
                    </div>
                    <div className="max-h-56 overflow-y-auto whitespace-pre-wrap break-words text-gray-700 dark:text-gray-200">
                      {test.data.result}
                    </div>
                    {!test.data.ok &&
                      /wam|authentication failed|apicontract|msal|sign|consent/i.test(
                        test.data.result
                      ) && (
                        <div className="mt-2 border-t border-red-200 pt-2 text-[11px] text-gray-500 dark:border-red-900/50">
                          <p className="font-medium text-gray-600 dark:text-gray-300">
                            This is a Microsoft 365 sign-in issue, not a MultiChat error.
                          </p>
                          <ul className="mt-0.5 list-disc space-y-0.5 pl-4">
                            <li>
                              A Microsoft sign-in window (WAM) opens in a separate console
                              on the server the first time — complete sign-in there, then
                              re-run the test.
                            </li>
                            <li>
                              WAM needs an interactive Windows session; it won't work if
                              the backend runs as a Windows service or over a
                              non-interactive session.
                            </li>
                            <li>
                              Your tenant admin must grant Work IQ consent — see{" "}
                              <a
                                href="https://github.com/microsoft/work-iq/blob/main/ADMIN-INSTRUCTIONS.md"
                                target="_blank"
                                rel="noreferrer"
                                className="text-blue-500 hover:underline"
                              >
                                admin instructions
                              </a>
                              .
                            </li>
                          </ul>
                        </div>
                      )}
                  </div>
                )}
              </div>
            )}

            {(connect.isError || data?.error) && (
              <p className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-xs text-red-600 dark:bg-red-950/40 dark:text-red-300">
                ⚠️ {(connect.error as Error)?.message || data?.error}
              </p>
            )}

            {isLoading ? (
              <p className="mt-3 text-xs text-gray-400">Loading…</p>
            ) : (
              connected &&
              data && (
                <div className="mt-4">
                  <p className="mb-1 text-xs font-medium text-gray-500">
                    Available M365 tools ({data.tools.length})
                  </p>
                  <div className="grid grid-cols-1 gap-1 sm:grid-cols-2">
                    {data.tools.map((t) => (
                      <div
                        key={t.name}
                        className="rounded border border-gray-200 px-2 py-1 text-xs dark:border-gray-700"
                        title={t.description}
                      >
                        <span className="font-medium">{t.raw_name}</span>
                        <span className="block truncate text-[10px] text-gray-400">
                          {t.description}
                        </span>
                      </div>
                    ))}
                    {data.tools.length === 0 && (
                      <span className="text-xs text-gray-400">
                        No tools discovered.
                      </span>
                    )}
                  </div>
                </div>
              )
            )}
          </div>

          <ToolsSection />
        </div>
      </div>

      {eulaOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
          onClick={() => !acceptEula.isPending && setEulaOpen(false)}
        >
          <div
            className="w-full max-w-md rounded-xl border border-gray-200 bg-white p-5 shadow-2xl dark:border-gray-700 dark:bg-gray-900"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-base font-semibold">Accept the Work IQ EULA</h3>
            <p className="mt-2 text-sm text-gray-500">
              To enable full access to Microsoft 365 through Work IQ, you must accept
              the End User License Agreement. This is a one-time action and can be done
              here, before you start chatting.
            </p>
            <a
              href={eulaUrl}
              target="_blank"
              rel="noreferrer"
              className="mt-2 inline-block break-all text-xs text-blue-500 hover:underline"
            >
              {eulaUrl}
            </a>
            {acceptEula.isError && (
              <p className="mt-2 rounded bg-red-50 px-2 py-1 text-xs text-red-600 dark:bg-red-950/40 dark:text-red-300">
                ⚠️ {(acceptEula.error as Error).message}
              </p>
            )}
            <div className="mt-4 flex justify-end gap-2">
              <button
                onClick={() => setEulaOpen(false)}
                disabled={acceptEula.isPending}
                className="rounded border border-gray-300 px-3 py-1.5 text-sm hover:bg-gray-50 disabled:opacity-40 dark:border-gray-600 dark:hover:bg-gray-800"
              >
                Cancel
              </button>
              <button
                onClick={() => acceptEula.mutate()}
                disabled={acceptEula.isPending}
                className="rounded bg-brand px-3 py-1.5 text-sm font-medium text-white hover:brightness-110 disabled:opacity-40"
              >
                {acceptEula.isPending ? "Accepting…" : "I accept the EULA"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
