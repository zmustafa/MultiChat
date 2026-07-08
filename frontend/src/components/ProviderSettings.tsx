import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { apiFetch, streamSSE } from "../api/client";
import type {
  AuthMethod,
  Provider,
  ProviderType,
  TestResult,
  ToolCredential,
} from "../api/types";
import { useProviders, useProviderMutations } from "../hooks/useProviders";
import { AddProviderWizard } from "./AddProviderWizard";
import { DiagnosticsPanel, type DiagStep } from "./DiagnosticsPanel";

export const TYPES: ProviderType[] = [
  "openai",
  "openai_eu",
  "azure_openai",
  "azure_foundry",
  "anthropic",
  "gemini",
  "ollama",
  "openai_compatible",
  "github_copilot",
];

export const OAUTH_CAPABLE: ProviderType[] = ["openai", "anthropic", "github_copilot"];

export interface FormState {
  name: string;
  provider_type: ProviderType;
  auth_method: AuthMethod;
  base_url: string;
  api_key: string;
  models: string;
  default_model: string;
  extra: string;
  is_default: boolean;
}

export const empty: FormState = {
  name: "",
  provider_type: "openai",
  auth_method: "api_key",
  base_url: "",
  api_key: "",
  models: "",
  default_model: "",
  extra: "",
  is_default: false,
};

function OAuthConnect({ provider }: { provider: Provider }) {
  const qc = useQueryClient();
  const [status, setStatus] = useState<string>("");
  const [mode, setMode] = useState<string>("");
  const [flavor, setFlavor] = useState<string>("");
  const [pasteCode, setPasteCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [deviceInfo, setDeviceInfo] = useState<{
    user_code?: string;
    verification_uri?: string;
  }>({});
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pollFnRef = useRef<() => void>(() => {});
  const pollingRef = useRef(false);
  const armTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const intervalMsRef = useRef(5000);

  // Begin the polling interval (idempotent). Polls once immediately, then on a timer.
  const runInterval = () => {
    if (pollRef.current) return;
    pollFnRef.current();
    pollRef.current = setInterval(() => pollFnRef.current(), intervalMsRef.current);
  };
  // Arm polling but don't hit the backend right away. We wait for the tab to regain focus
  // (the focus handler starts the interval) — i.e. when the user returns from the OAuth
  // browser tab. A delayed fallback starts polling anyway if no focus event arrives.
  const armPolling = (intervalMs: number) => {
    intervalMsRef.current = intervalMs;
    pollingRef.current = true;
    if (armTimerRef.current) clearTimeout(armTimerRef.current);
    armTimerRef.current = setTimeout(runInterval, 2000);
  };
  const stopPolling = () => {
    pollingRef.current = false;
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    if (armTimerRef.current) {
      clearTimeout(armTimerRef.current);
      armTimerRef.current = null;
    }
  };

  // Clear the interval when the component unmounts (e.g. switching providers).
  useEffect(() => () => stopPolling(), []);

  // Resume an in-flight device sign-in after a page reload or component remount: the
  // backend still has the pending flow, so keep polling until it's authorized instead of
  // stalling at "not connected" forever.
  useEffect(() => {
    if (provider.oauth_connected || !provider.oauth_pending || pollingRef.current) return;
    setMode("device");
    setStatus("Finishing sign-in… waiting for GitHub. Keep this tab open.");
    armPolling(5000);
    // The user just reloaded and is looking at this tab, so start polling right away.
    if (document.hasFocus()) runInterval();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [provider.oauth_pending, provider.oauth_connected]);

  // Poll immediately when the user returns to this tab (e.g. right after authorizing on
  // github.com/login/device). Background tabs throttle timers to ~once/minute, so without
  // this the "connected" state can take up to a minute to be noticed after authorizing.
  useEffect(() => {
    const onFocus = () => {
      if (
        !pollingRef.current ||
        provider.oauth_connected ||
        document.visibilityState === "hidden"
      )
        return;
      // Focus regained (user returned from the OAuth tab): cancel the fallback timer,
      // poll immediately to catch up, and make sure the interval is running.
      if (armTimerRef.current) {
        clearTimeout(armTimerRef.current);
        armTimerRef.current = null;
      }
      pollFnRef.current();
      runInterval();
    };
    document.addEventListener("visibilitychange", onFocus);
    window.addEventListener("focus", onFocus);
    return () => {
      document.removeEventListener("visibilitychange", onFocus);
      window.removeEventListener("focus", onFocus);
    };
  }, [provider.oauth_connected]);

  async function start() {
    setBusy(true);
    setStatus("Starting sign-in…");
    setPasteCode("");
    try {
      const res = await apiFetch<any>(`/api/providers/${provider.id}/oauth/start`, {
        method: "POST",
      });
      setMode(res.mode);
      setFlavor(res.flavor || "");
      if (res.authorize_url) window.open(res.authorize_url, "_blank", "noopener");
      if (res.mode === "device") {
        setDeviceInfo({
          user_code: res.user_code,
          verification_uri: res.verification_uri,
        });
        setStatus("Enter the code shown below in your browser, then wait…");
        armPolling((res.interval || 5) * 1000);
      } else if (res.mode === "loopback") {
        setStatus("A browser window opened — sign in and it will connect automatically.");
        armPolling(3000);
      } else {
        setStatus("Sign in via the opened browser tab, then paste the code below.");
      }
    } catch (e) {
      setStatus(`Error: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  async function poll() {
    try {
      const res = await apiFetch<{ status: string; detail?: string }>(
        `/api/providers/${provider.id}/oauth/poll`,
        { method: "POST" }
      );
      if (res.status === "authorized") {
        stopPolling();
        setStatus("Connected ✓");
        finishConnected();
      } else if (res.status === "error") {
        stopPolling();
        setStatus(`Error: ${res.detail}`);
      }
    } catch (e) {
      // Stop if the provider was deleted (or the flow no longer exists) so we don't spam
      // the backend with a poll that can never succeed. Transient network blips are ignored.
      const msg = (e as Error).message || "";
      if (/not found/i.test(msg) || /no pending flow/i.test(msg)) stopPolling();
      /* otherwise keep polling */
    }
  }

  async function complete() {
    if (!pasteCode.trim()) return;
    setBusy(true);
    setStatus("Completing sign-in…");
    try {
      const res = await apiFetch<{ status: string; detail?: string }>(
        `/api/providers/${provider.id}/oauth/complete`,
        { method: "POST", body: JSON.stringify({ code: pasteCode.trim() }) }
      );
      if (res.status === "authorized") finishConnected();
      else setStatus(`Error: ${res.detail}`);
    } catch (e) {
      setStatus(`Error: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  async function disconnect() {
    stopPolling();
    setBusy(true);
    try {
      await apiFetch<void>(`/api/providers/${provider.id}/oauth/disconnect`, {
        method: "POST",
      });
      // Reset local sign-in state and refresh the providers list in place so the panel
      // flips to "not connected" without a full-page reload (which steals focus).
      setMode("");
      setFlavor("");
      setPasteCode("");
      setDeviceInfo({});
      setStatus("");
      await qc.invalidateQueries({ queryKey: ["providers"] });
    } catch (e) {
      setStatus(`Error: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  async function finishConnected() {
    // Populate the model list so lanes can pick a model right away.
    try {
      await apiFetch(`/api/providers/${provider.id}/models`);
    } catch {
      /* non-fatal */
    }
    // Refresh the providers list in place (no full-page reload) so the panel flips to
    // "connected" while keeping the current provider selected and focused.
    setMode("");
    setFlavor("");
    setPasteCode("");
    setDeviceInfo({});
    setStatus("Connected ✓");
    await qc.invalidateQueries({ queryKey: ["providers"] });
  }

  const connecting = !!mode && !provider.oauth_connected;
  const pasteLabel =
    flavor === "claude"
      ? "Paste the code from the Anthropic page (looks like code#state)"
      : "Didn't connect automatically? Paste the full callback URL here";
  const placeholder = flavor === "claude" ? "code#state" : "http://localhost:1455/auth/callback?code=…";

  // Keep the ref pointing at the latest poll so the mount/focus effects call a fresh closure.
  pollFnRef.current = poll;

  return (
    <div className="mt-2 rounded border border-gray-200 bg-gray-50 p-2 text-xs dark:border-gray-700 dark:bg-gray-800">
      <div className="mb-1 flex items-center justify-between">
        <span className="font-semibold">
          OAuth:{" "}
          {provider.oauth_connected ? (
            <span className="text-green-600">connected ✓</span>
          ) : (
            <span className="text-gray-500">not connected</span>
          )}
        </span>
        {provider.oauth_connected ? (
          <button
            type="button"
            onClick={disconnect}
            disabled={busy}
            className="rounded bg-red-100 px-2 py-1 text-red-700 disabled:opacity-50 dark:bg-red-950 dark:text-red-300"
          >
            {busy ? "Disconnecting…" : "Disconnect"}
          </button>
        ) : (
          <button
            onClick={start}
            disabled={busy}
            className="rounded-lg bg-brand px-3 py-1 text-white hover:brightness-110 disabled:opacity-50"
          >
            {connecting ? "Restart sign-in" : "Connect"}
          </button>
        )}
      </div>

      {/* Device flow (GitHub Copilot): show the user code prominently. */}
      {deviceInfo.user_code && !provider.oauth_connected && (
        <div className="my-2 rounded bg-white p-2 text-center dark:bg-gray-900">
          <div className="text-gray-500">Enter this code at</div>
          <a
            className="text-blue-500 underline"
            href={deviceInfo.verification_uri}
            target="_blank"
            rel="noreferrer"
          >
            {deviceInfo.verification_uri}
          </a>
          <div className="mt-1 flex items-center justify-center gap-2">
            <span className="rounded bg-gray-100 px-3 py-1 font-mono text-base tracking-widest dark:bg-gray-800">
              {deviceInfo.user_code}
            </span>
            <button
              onClick={() => navigator.clipboard.writeText(deviceInfo.user_code || "")}
              className="rounded border border-gray-300 px-2 py-1 dark:border-gray-600"
            >
              Copy
            </button>
          </div>
        </div>
      )}

      {/* Paste fallback (Claude always; ChatGPT loopback as a fallback). */}
      {connecting && mode !== "device" && (
        <div className="mt-1">
          <div className="mb-1 text-gray-500">{pasteLabel}</div>
          <div className="flex gap-1">
            <input
              value={pasteCode}
              onChange={(e) => setPasteCode(e.target.value)}
              placeholder={placeholder}
              className="flex-1 rounded border border-gray-300 px-2 py-1 dark:border-gray-600 dark:bg-gray-900"
            />
            <button
              onClick={complete}
              disabled={busy || !pasteCode.trim()}
              className="rounded-lg bg-brand px-3 text-white hover:brightness-110 disabled:opacity-50"
            >
              Submit
            </button>
          </div>
        </div>
      )}

      {status && <div className="mt-1.5 text-gray-500">{status}</div>}
    </div>
  );
}

export function ToolsSection() {
  const [cred, setCred] = useState<ToolCredential | null>(null);
  const [key, setKey] = useState("");
  const [engine, setEngine] = useState<"brave" | "duckduckgo">("brave");
  const [msg, setMsg] = useState("");
  const [ok, setOk] = useState<boolean | null>(null);

  useEffect(() => {
    apiFetch<ToolCredential[]>("/api/tools/credentials").then((rows) => {
      const c = rows.find((c) => c.tool === "web_search") || null;
      setCred(c);
      const e = (c?.extra?.engine as string) || (c?.has_key ? "brave" : "duckduckgo");
      setEngine(e === "duckduckgo" ? "duckduckgo" : "brave");
    });
  }, []);

  async function persist(nextEngine: "brave" | "duckduckgo", apiKey?: string) {
    const res = await apiFetch<ToolCredential>("/api/tools/credentials", {
      method: "PUT",
      body: JSON.stringify({
        tool: "web_search",
        api_key: apiKey || undefined,
        extra: { engine: nextEngine },
      }),
    });
    setCred(res);
  }

  async function changeEngine(next: "brave" | "duckduckgo") {
    setEngine(next);
    await persist(next);
    setOk(true);
    setMsg(
      next === "duckduckgo"
        ? "Using DuckDuckGo (no API key required)."
        : "Using Brave Search."
    );
  }

  async function save() {
    await persist(engine, key);
    setKey("");
    setOk(true);
    setMsg("Saved.");
  }

  async function test() {
    const res = await apiFetch<TestResult>("/api/tools/search/test", {
      method: "POST",
    });
    setOk(res.ok);
    setMsg(res.detail);
  }

  return (
    <section className="rounded-lg border border-gray-200 bg-white shadow-sm dark:border-gray-700 dark:bg-gray-900">
      <div className="border-b border-gray-200 px-5 py-4 dark:border-gray-700">
        <h2 className="font-medium">Tools</h2>
        <p className="mt-0.5 text-xs text-gray-500">
          Configure the web_search tool. Choose a search engine — DuckDuckGo works
          with no API key; Brave Search needs a key.
        </p>
      </div>
      <div className="p-5">
        <label className="mb-1 block text-xs font-medium text-gray-500">
          Search engine
        </label>
        <select
          value={engine}
          onChange={(e) => changeEngine(e.target.value as "brave" | "duckduckgo")}
          className="mb-4 w-full rounded-lg border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800"
        >
          <option value="duckduckgo">DuckDuckGo (no API key)</option>
          <option value="brave">Brave Search (API key)</option>
        </select>

        <label className="mb-1 block text-xs font-medium text-gray-500">
          Brave Search API key
          {engine === "duckduckgo" && (
            <span className="ml-1 font-normal text-gray-400">
              (only used when Brave is selected)
            </span>
          )}
        </label>
        <div className="text-xs text-gray-400">
          {cred?.has_key ? `Configured · ${cred.masked_key}` : "No key configured"}
        </div>
        <div className="mt-2 flex gap-2">
          <input
            type="password"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder="Brave Search API key"
            className="flex-1 rounded-lg border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800"
          />
          <button
            onClick={save}
            className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white hover:brightness-110"
          >
            Save
          </button>
          <button
            onClick={test}
            className="rounded-lg border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-600"
          >
            Test
          </button>
        </div>
        {msg && (
          <div
            className={`mt-2 rounded-md border px-3 py-1.5 text-xs ${
              ok
                ? "border-green-200 bg-green-50 text-green-700 dark:border-green-900 dark:bg-green-950/40 dark:text-green-400"
                : "border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-400"
            }`}
          >
            {msg}
          </div>
        )}
      </div>
    </section>
  );
}

export const PROVIDER_META: Record<
  ProviderType,
  { label: string; hint: string; group: string }
> = {
  openai: {
    label: "OpenAI",
    hint: "API key from platform.openai.com — or sign in with your ChatGPT account.",
    group: "OpenAI / Azure",
  },
  azure_openai: {
    label: "Azure OpenAI",
    hint: "Set the endpoint (base URL), api-version and deployment in the extra JSON.",
    group: "OpenAI / Azure",
  },
  azure_foundry: {
    label: "Azure Foundry",
    hint: "Azure AI Foundry inference — endpoint (…services.ai.azure.com), key & api-version. Model = a deployed model name.",
    group: "OpenAI / Azure",
  },
  github_copilot: {
    label: "GitHub Copilot",
    hint: "Sign in with a GitHub account that has an active Copilot subscription.",
    group: "OpenAI / Azure",
  },
  anthropic: {
    label: "Anthropic Claude",
    hint: "API key (sk-ant-…) — or sign in with your Claude Pro/Max subscription.",
    group: "Other providers",
  },
  gemini: {
    label: "Google Gemini",
    hint: "API key from Google AI Studio (aistudio.google.com).",
    group: "Other providers",
  },
  openai_compatible: {
    label: "OpenAI-compatible",
    hint: "Any OpenAI-compatible gateway — OpenRouter, Together, Groq, vLLM…",
    group: "Other providers",
  },
  openai_eu: {
    label: "OpenAI (EU)",
    hint: "EU data residency — routes to eu.api.openai.com (use an EU-enabled OpenAI key).",
    group: "OpenAI / Azure",
  },
  ollama: {
    label: "Ollama (local)",
    hint: "Point the base URL at your local Ollama server. Usually no API key.",
    group: "Local",
  },
};

export const GROUPS = ["OpenAI / Azure", "Other providers", "Local"];

function StatusDot({ color, title }: { color: string; title: string }) {
  return <span className={`inline-block h-2 w-2 rounded-full ${color}`} title={title} />;
}

export function ProviderSettings() {
  const providersQuery = useProviders();
  const providers = providersQuery.data ?? [];
  const { create, update, remove } = useProviderMutations();
  const [selected, setSelected] = useState<string | "new" | null>(null);
  const [form, setForm] = useState<FormState>(empty);
  const [wizardOpen, setWizardOpen] = useState(false);

  // Staged diagnostics for Test connection + model refresh.
  const [testSteps, setTestSteps] = useState<DiagStep[]>([]);
  const [testResult, setTestResult] = useState<{ ok: boolean; detail: string } | null>(null);
  const [testing, setTesting] = useState(false);
  const [refreshSteps, setRefreshSteps] = useState<DiagStep[]>([]);
  const [refreshResult, setRefreshResult] = useState<{ ok: boolean; detail: string } | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  // Default the selection once providers have loaded (first provider, or the add form).
  useEffect(() => {
    if (selected === null && providersQuery.isSuccess) {
      setSelected(providers.length ? providers[0].id : null);
    }
  }, [providersQuery.isSuccess, providers, selected]);

  // Reset diagnostics when switching providers.
  useEffect(() => {
    setTestSteps([]);
    setTestResult(null);
    setTesting(false);
    setRefreshSteps([]);
    setRefreshResult(null);
    setRefreshing(false);
  }, [selected]);

  // Seed the right-pane form whenever the selection changes.
  useEffect(() => {
    if (selected === "new") {
      setForm(empty);
      return;
    }
    const p = providers.find((x) => x.id === selected);
    if (p) {
      setForm({
        name: p.name,
        provider_type: p.provider_type,
        auth_method: p.auth_method,
        base_url: p.base_url || "",
        api_key: "",
        models: (p.models || []).join(", "),
        default_model: p.default_model || "",
        extra: Object.keys(p.extra || {}).length ? JSON.stringify(p.extra) : "",
        is_default: p.is_default,
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected]);

  const isNew = selected === "new";
  const selectedProvider = providers.find((x) => x.id === selected);

  function buildBody() {
    let extra: Record<string, unknown> = {};
    if (form.extra.trim()) {
      try {
        extra = JSON.parse(form.extra);
      } catch {
        alert("Extra must be valid JSON");
        throw new Error("bad json");
      }
    }
    return {
      name: form.name,
      provider_type: form.provider_type,
      auth_method: form.auth_method,
      base_url: form.base_url || null,
      api_key: form.api_key || undefined,
      models: form.models
        ? form.models.split(",").map((s) => s.trim()).filter(Boolean)
        : [],
      default_model: form.default_model || null,
      extra,
      is_default: form.is_default,
    };
  }

  async function submit() {
    try {
      const body = buildBody();
      if (isNew) {
        const created = await create.mutateAsync(body as any);
        setSelected(created.id);
      } else if (selected) {
        await update.mutateAsync({ id: selected, body });
      }
    } catch {
      /* handled */
    }
  }

  async function runTest(id: string) {
    setTesting(true);
    setTestSteps([]);
    setTestResult(null);
    streamSSE(
      `/api/providers/${id}/test/stream`,
      {},
      (evt) => {
        if (evt.event === "step") {
          setTestSteps((s) => [...s.filter((x) => x.step !== evt.data.step), evt.data]);
        } else if (evt.event === "done") {
          setTestResult({ ok: evt.data.ok, detail: evt.data.detail });
          setTesting(false);
        }
      },
      () => setTesting(false),
      () => setTesting(false)
    );
  }

  function runRefresh(id: string) {
    setRefreshing(true);
    setRefreshSteps([]);
    setRefreshResult(null);
    streamSSE(
      `/api/providers/${id}/models/stream`,
      {},
      (evt) => {
        if (evt.event === "step") {
          setRefreshSteps((s) => [...s.filter((x) => x.step !== evt.data.step), evt.data]);
        } else if (evt.event === "done") {
          setRefreshResult({ ok: evt.data.ok, detail: evt.data.detail });
          setRefreshing(false);
          if (evt.data.models?.length) {
            setForm((f) => ({ ...f, models: evt.data.models.join(", ") }));
            providersQuery.refetch();
          }
        }
      },
      () => setRefreshing(false),
      () => setRefreshing(false)
    );
  }

  const showBaseUrl = [
    "azure_openai",
    "azure_foundry",
    "ollama",
    "openai_compatible",
  ].includes(form.provider_type);
  const meta = PROVIDER_META[form.provider_type];
  const modelList = form.models
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);

  return (
    <div className="mx-auto max-w-5xl space-y-5 p-4">
      {providersQuery.isSuccess && providers.length === 0 && (
        <div className="rounded-lg border border-amber-300 bg-amber-50 px-5 py-4 dark:border-amber-800 dark:bg-amber-950/30">
          <div className="flex items-start gap-3">
            <span className="text-xl leading-none">⚠️</span>
            <div className="min-w-0 flex-1">
              <h3 className="text-sm font-semibold text-amber-800 dark:text-amber-200">
                Set up an AI provider to get started
              </h3>
              <p className="mt-0.5 text-xs text-amber-700 dark:text-amber-300/90">
                You don't have any AI providers configured yet. Nothing will work until you
                add one — connect a provider below to start chatting, comparing models, and
                running evaluations.
              </p>
              <button
                onClick={() => setWizardOpen(true)}
                className="mt-2 rounded-lg bg-amber-600 px-3 py-1.5 text-sm font-medium text-white hover:brightness-110"
              >
                ＋ Add your first provider
              </button>
            </div>
          </div>
        </div>
      )}

      <section className="rounded-lg border border-gray-200 bg-white shadow-sm dark:border-gray-700 dark:bg-gray-900">
        <div className="border-b border-gray-200 px-5 py-4 dark:border-gray-700">
          <h2 className="font-medium">AI Providers</h2>
          <p className="mt-0.5 text-xs text-gray-500">
            Add the providers that power your lanes, connect credentials, and choose models.
            Keys are encrypted at rest and never returned in full.
          </p>
        </div>

        <div className="flex flex-col md:flex-row">
          {/* Provider rail */}
          <div className="w-full shrink-0 space-y-2 border-b border-gray-200 p-2 md:w-56 md:border-b-0 md:border-r dark:border-gray-700">
            <button
              onClick={() => setWizardOpen(true)}
              className="flex w-full items-center justify-center gap-2 rounded-lg bg-brand px-3 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:brightness-110"
            >
              <span className="text-base leading-none">＋</span> Add provider
            </button>

            {GROUPS.map((group) => {
              const items = providers.filter(
                (p) => PROVIDER_META[p.provider_type]?.group === group
              );
              if (items.length === 0) return null;
              return (
                <div key={group} className="space-y-1">
                  <div className="px-3 pt-1 text-[10px] font-semibold uppercase tracking-wide text-gray-400">
                    {group}
                  </div>
                  {items.map((p) => {
                    const viewing = selected === p.id;
                    return (
                      <button
                        key={p.id}
                        onClick={() => setSelected(p.id)}
                        className={`flex w-full items-center justify-between gap-2 rounded-lg px-3 py-2 text-left text-sm transition ${
                          viewing
                            ? "bg-brand/10 font-medium text-brand"
                            : "text-gray-700 hover:bg-gray-100 dark:text-gray-200 dark:hover:bg-gray-800"
                        }`}
                      >
                        <span className="min-w-0 flex-1">
                          <span className="block truncate">{p.name}</span>
                          <span
                            className={`block truncate text-[11px] font-normal ${
                              viewing ? "text-brand/70" : "text-gray-400"
                            }`}
                          >
                            {PROVIDER_META[p.provider_type]?.label || p.provider_type}
                          </span>
                        </span>
                        <span className="flex shrink-0 items-center gap-1">
                          {p.is_default && (
                            <span className="rounded-full bg-green-100 px-1.5 py-0.5 text-[10px] font-medium text-green-700 dark:bg-green-900/50 dark:text-green-300">
                              default
                            </span>
                          )}
                          {p.auth_method === "oauth" ? (
                            <StatusDot
                              color={p.oauth_connected ? "bg-green-500" : "bg-gray-300"}
                              title={p.oauth_connected ? "Connected" : "Not connected"}
                            />
                          ) : (
                            p.has_key && (
                              <StatusDot color="bg-green-500" title="Configured" />
                            )
                          )}
                        </span>
                      </button>
                    );
                  })}
                </div>
              );
            })}

            {providers.length === 0 && (
              <div className="px-3 py-2 text-xs text-gray-400">
                No providers yet. Click “Add provider”.
              </div>
            )}
          </div>

          {/* Config pane */}
          <div className="min-w-0 flex-1 p-5">
            {!selectedProvider ? (
              <div className="flex flex-col items-center justify-center gap-3 py-12 text-center">
                <div className="text-4xl">🔌</div>
                <p className="max-w-xs text-sm text-gray-500">
                  {providers.length
                    ? "Select a provider on the left to configure it, or add a new one."
                    : "No providers yet. Add your first provider to power your lanes."}
                </p>
                <button
                  onClick={() => setWizardOpen(true)}
                  className="rounded-lg bg-brand px-4 py-1.5 text-sm font-medium text-white hover:brightness-110"
                >
                  ＋ Add provider
                </button>
              </div>
            ) : (
            <div className="space-y-4">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <h3 className="text-sm font-semibold text-gray-800 dark:text-gray-100">
                    {isNew ? "Add a provider" : selectedProvider?.name}
                  </h3>
                  <p className="mt-0.5 text-xs text-gray-500">{meta?.hint}</p>
                </div>
                {!isNew && selectedProvider && (
                  <div className="flex shrink-0 items-center gap-2">
                    <button
                      onClick={() => runTest(selectedProvider.id)}
                      disabled={testing}
                      className="rounded-lg border border-gray-300 px-3 py-1.5 text-xs font-medium hover:bg-gray-50 disabled:opacity-50 dark:border-gray-600 dark:hover:bg-gray-800"
                    >
                      {testing ? "Testing…" : "Test connection"}
                    </button>
                    <button
                      onClick={() => {
                        if (confirm(`Delete ${selectedProvider.name}?`)) {
                          remove.mutate(selectedProvider.id);
                          setSelected(null);
                        }
                      }}
                      className="rounded-lg border border-red-200 px-3 py-1.5 text-xs font-medium text-red-600 hover:bg-red-50 dark:border-red-900 dark:hover:bg-red-950/40"
                    >
                      Delete
                    </button>
                  </div>
                )}
              </div>

              {!isNew && (testing || testSteps.length > 0 || testResult) && (
                <DiagnosticsPanel
                  running={testing}
                  steps={testSteps}
                  result={testResult ?? undefined}
                  variant="test"
                />
              )}

              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <div>
                  <label className="mb-1 block text-xs font-medium text-gray-500">
                    Display name
                  </label>
                  <input
                    placeholder="e.g. My OpenAI"
                    value={form.name}
                    onChange={(e) => setForm({ ...form, name: e.target.value })}
                    className="w-full rounded-lg border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-xs font-medium text-gray-500">
                    Provider type
                  </label>
                  {isNew ? (
                    <select
                      value={form.provider_type}
                      onChange={(e) =>
                        setForm({
                          ...form,
                          provider_type: e.target.value as ProviderType,
                          auth_method: "api_key",
                        })
                      }
                      className="w-full rounded-lg border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800"
                    >
                      {TYPES.map((t) => (
                        <option key={t} value={t}>
                          {PROVIDER_META[t].label}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <div className="rounded-lg border border-gray-200 bg-gray-50 px-3 py-1.5 text-sm text-gray-600 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300">
                      {meta?.label}
                    </div>
                  )}
                </div>

                {OAUTH_CAPABLE.includes(form.provider_type) && (
                  <div>
                    <label className="mb-1 block text-xs font-medium text-gray-500">
                      Authentication
                    </label>
                    <select
                      value={form.auth_method}
                      onChange={(e) =>
                        setForm({ ...form, auth_method: e.target.value as AuthMethod })
                      }
                      className="w-full rounded-lg border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800"
                    >
                      <option value="api_key">API key</option>
                      <option value="oauth">OAuth sign-in</option>
                    </select>
                  </div>
                )}

                {showBaseUrl && (
                  <div>
                    <label className="mb-1 block text-xs font-medium text-gray-500">
                      Base URL
                    </label>
                    <input
                      placeholder="https://…"
                      value={form.base_url}
                      onChange={(e) => setForm({ ...form, base_url: e.target.value })}
                      autoComplete="off"
                      data-1p-ignore
                      data-lpignore="true"
                      className="w-full rounded-lg border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800"
                    />
                  </div>
                )}

                {form.auth_method === "api_key" && (
                  <div>
                    <label className="mb-1 block text-xs font-medium text-gray-500">
                      API key{" "}
                      {!isNew && selectedProvider?.has_key && (
                        <span className="font-normal text-gray-400">
                          · {selectedProvider.masked_key}
                        </span>
                      )}
                    </label>
                    <input
                      type="password"
                      placeholder={!isNew ? "leave blank to keep current" : "sk-…"}
                      value={form.api_key}
                      onChange={(e) => setForm({ ...form, api_key: e.target.value })}
                      autoComplete="new-password"
                      data-1p-ignore
                      data-lpignore="true"
                      className="w-full rounded-lg border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800"
                    />
                  </div>
                )}
              </div>

              <div>
                <div className="mb-1 flex items-center justify-between">
                  <label className="text-xs font-medium text-gray-500">
                    Models{" "}
                    <span className="font-normal text-gray-400">
                      ({modelList.length})
                    </span>
                  </label>
                  {!isNew && selectedProvider && (
                    <button
                      onClick={() => runRefresh(selectedProvider.id)}
                      disabled={refreshing}
                      className="text-xs font-medium text-brand hover:underline disabled:opacity-50"
                    >
                      {refreshing ? "Refreshing…" : "↻ Refresh models"}
                    </button>
                  )}
                </div>

                {modelList.length > 0 && (
                  <>
                    {/* Browse the retrieved models; picking one sets it as the default. */}
                    <select
                      value=""
                      onChange={(e) => {
                        if (e.target.value)
                          setForm({ ...form, default_model: e.target.value });
                      }}
                      className="mb-2 w-full rounded-lg border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800"
                    >
                      <option value="">
                        Browse {modelList.length} models — pick to set as default…
                      </option>
                      {modelList.map((m) => (
                        <option key={m} value={m}>
                          {m === form.default_model ? "★ " : ""}
                          {m}
                        </option>
                      ))}
                    </select>

                    {/* Chips: click any model to make it the default (★ marks current). */}
                    <div className="mb-2 flex max-h-40 flex-wrap gap-1 overflow-y-auto rounded-lg border border-gray-200 p-2 dark:border-gray-700">
                      {modelList.map((m) => (
                        <button
                          key={m}
                          type="button"
                          onClick={() => setForm({ ...form, default_model: m })}
                          title={
                            m === form.default_model
                              ? "Default model"
                              : "Click to set as default"
                          }
                          className={`rounded-full border px-2 py-0.5 text-xs transition ${
                            m === form.default_model
                              ? "border-brand bg-brand/10 font-medium text-brand"
                              : "border-gray-300 text-gray-600 hover:border-brand hover:text-brand dark:border-gray-600 dark:text-gray-300"
                          }`}
                        >
                          {m === form.default_model && "★ "}
                          {m}
                        </button>
                      ))}
                    </div>
                  </>
                )}

                <input
                  placeholder="comma-separated model ids"
                  value={form.models}
                  onChange={(e) => setForm({ ...form, models: e.target.value })}
                  className="w-full rounded-lg border border-gray-300 px-3 py-1.5 text-xs text-gray-500 dark:border-gray-600 dark:bg-gray-800"
                />
                <p className="mt-0.5 text-[10px] text-gray-400">
                  Edit the raw list above (comma-separated) to add or remove models.
                </p>
                {!isNew && (refreshing || refreshSteps.length > 0 || refreshResult) && (
                  <DiagnosticsPanel
                    running={refreshing}
                    steps={refreshSteps}
                    result={refreshResult ?? undefined}
                    title="Model catalogue refresh"
                    variant="models"
                  />
                )}
              </div>

              <div>
                <label className="mb-1 block text-xs font-medium text-gray-500">
                  Default model
                </label>
                <select
                  value={form.default_model}
                  onChange={(e) => setForm({ ...form, default_model: e.target.value })}
                  className="w-full rounded-lg border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800"
                >
                  <option value="">— none —</option>
                  {form.default_model &&
                    !modelList.includes(form.default_model) && (
                      <option value={form.default_model}>{form.default_model}</option>
                    )}
                  {modelList.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>
                {form.default_model && (
                  <p className="mt-0.5 text-[10px] text-gray-400">
                    New lanes for this provider will default to{" "}
                    <span className="font-medium text-gray-500">
                      {form.default_model}
                    </span>
                    .
                  </p>
                )}
              </div>

              <div>
                <label className="mb-1 block text-xs font-medium text-gray-500">
                  Advanced (extra JSON)
                </label>
                <textarea
                  placeholder='{"api_version":"2024-08-01-preview","deployment":"gpt-4o"}'
                  value={form.extra}
                  onChange={(e) => setForm({ ...form, extra: e.target.value })}
                  rows={2}
                  className="w-full rounded-lg border border-gray-300 px-3 py-1.5 font-mono text-xs dark:border-gray-600 dark:bg-gray-800"
                />
              </div>

              <label className="flex items-center gap-2 text-xs text-gray-600 dark:text-gray-300">
                <input
                  type="checkbox"
                  checked={form.is_default}
                  onChange={(e) => setForm({ ...form, is_default: e.target.checked })}
                />
                Set as the default provider
              </label>

              <div className="flex items-center gap-2 border-t border-gray-100 pt-3 dark:border-gray-800">
                <button
                  onClick={submit}
                  disabled={create.isPending || update.isPending || !form.name}
                  className="rounded-lg bg-brand px-4 py-1.5 text-sm font-medium text-white hover:brightness-110 disabled:opacity-50"
                >
                  {isNew ? "Add provider" : "Save changes"}
                </button>
                {isNew && providers.length > 0 && (
                  <button
                    onClick={() => setSelected(providers[0].id)}
                    className="rounded-lg border border-gray-300 px-4 py-1.5 text-sm dark:border-gray-600"
                  >
                    Cancel
                  </button>
                )}
              </div>

              {/* OAuth sign-in */}
              {!isNew && selectedProvider?.auth_method === "oauth" && (
                <OAuthConnect key={selectedProvider.id} provider={selectedProvider} />
              )}
              {isNew && form.auth_method === "oauth" && (
                <div className="rounded-md border border-sky-200 bg-sky-50 px-3 py-2 text-xs text-sky-700 dark:border-sky-900 dark:bg-sky-950/40 dark:text-sky-300">
                  Add the provider first, then a <b>Connect</b> button will appear here to sign in.
                </div>
              )}
            </div>
            )}
          </div>
        </div>
      </section>

      {wizardOpen && (
        <AddProviderWizard
          onClose={() => setWizardOpen(false)}
          onCreated={(id) => {
            setWizardOpen(false);
            setSelected(id);
          }}
        />
      )}
    </div>
  );
}
