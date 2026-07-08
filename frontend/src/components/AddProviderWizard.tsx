import { useRef, useState } from "react";
import type { AuthMethod, ProviderType } from "../api/types";
import { useProviderMutations } from "../hooks/useProviders";
import { useDismiss } from "../hooks/useDismiss";
import {
  GROUPS,
  OAUTH_CAPABLE,
  PROVIDER_META,
  TYPES,
  empty,
  type FormState,
} from "./ProviderSettings";

/** Real (SVG) brand-style icons for each provider type. */
function ProviderIcon({
  type,
  className = "h-6 w-6",
}: {
  type: ProviderType;
  className?: string;
}) {
  switch (type) {
    case "openai":
    case "openai_eu":
      return (
        <svg viewBox="0 0 24 24" className={className} fill="#10A37F" aria-hidden="true">
          <path d="M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.1419.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z" />
        </svg>
      );
    case "azure_openai":
    case "azure_foundry":
      return (
        <svg viewBox="0 0 24 24" className={className} aria-hidden="true">
          <path
            fill="#0078D4"
            d="M14.7 3.4 8.2 21h4.4l1.2-3.6h-3l2.7-7.9 4.1 12h4.9L14.7 3.4z"
          />
          <path fill="#3399E6" d="M9.7 3.4 3 21h4.5l6.8-17.6z" opacity="0.75" />
        </svg>
      );
    case "github_copilot":
      return (
        <svg viewBox="0 0 24 24" className={className} fill="currentColor" aria-hidden="true">
          <path d="M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12" />
        </svg>
      );
    case "anthropic":
      return (
        <svg viewBox="0 0 24 24" className={className} fill="#D97757" aria-hidden="true">
          <path d="M17.3041 3.541h-3.6718l6.696 16.918H24Zm-10.6082 0L0 20.459h3.7442l1.3693-3.5527h7.0052l1.3693 3.5528h3.7442L10.5363 3.5409Zm-.3712 10.2232 2.2914-5.9456 2.2914 5.9456Z" />
        </svg>
      );
    case "gemini":
      return (
        <svg viewBox="0 0 24 24" className={className} aria-hidden="true">
          <defs>
            <linearGradient id="mc-gemini-grad" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0" stopColor="#4285F4" />
              <stop offset="0.5" stopColor="#9B72CB" />
              <stop offset="1" stopColor="#D96570" />
            </linearGradient>
          </defs>
          <path
            fill="url(#mc-gemini-grad)"
            d="M12 24A14.304 14.304 0 0 0 0 12 14.304 14.304 0 0 0 12 0a14.305 14.305 0 0 0 12 12 14.305 14.305 0 0 0-12 12"
          />
        </svg>
      );
    case "openai_compatible":
      return (
        <svg
          viewBox="0 0 24 24"
          className={className}
          fill="none"
          stroke="#6366F1"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M12 22v-5" />
          <path d="M9 8V2" />
          <path d="M15 8V2" />
          <path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8z" />
        </svg>
      );
    case "ollama":
      return (
        <svg viewBox="0 0 24 24" className={className} fill="currentColor" aria-hidden="true">
          <path d="M6.6 2.2c.55-.17 1.13.14 1.3.7l.86 2.85a8.9 8.9 0 0 1 6.48 0l.86-2.85c.17-.56.75-.87 1.3-.7.55.17.86.75.7 1.3l-.77 2.55c1.4 1.02 2.3 2.55 2.3 4.35v1.7c0 1.2-.5 2.3-1.3 3.13.83.83 1.3 1.95 1.3 3.12v1.35c0 .6-.48 1.1-1.08 1.1h-2.9v-1.6c0-.5-.4-.9-.9-.9s-.9.4-.9.9v1.6h-2.9v-1.6c0-.5-.4-.9-.9-.9s-.9.4-.9.9v1.6H6.5c-.6 0-1.08-.5-1.08-1.1V18.5c0-1.17.47-2.29 1.3-3.12A4.4 4.4 0 0 1 5.42 12.25v-1.7c0-1.8.9-3.33 2.3-4.35L6.95 3.5a1.02 1.02 0 0 1 .7-1.3zM9 9.3c-.66 0-1.2.6-1.2 1.35s.54 1.35 1.2 1.35 1.2-.6 1.2-1.35S9.66 9.3 9 9.3zm6 0c-.66 0-1.2.6-1.2 1.35s.54 1.35 1.2 1.35 1.2-.6 1.2-1.35S15.66 9.3 15 9.3zm-3 3.4c-1 0-1.85.5-2.2 1.25-.13.28.08.6.4.6h3.6c.32 0 .53-.32.4-.6-.35-.75-1.2-1.25-2.2-1.25z" />
        </svg>
      );
  }
}

const needsBaseUrl = (t: ProviderType) =>
  ["azure_openai", "azure_foundry", "ollama", "openai_compatible"].includes(t);

/** Guided modal for adding a new provider: pick a type, then enter credentials. */
export function AddProviderWizard({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (id: string) => void;
}) {
  const { create } = useProviderMutations();
  const [step, setStep] = useState<1 | 2>(1);
  const [form, setForm] = useState<FormState>(empty);
  const [advanced, setAdvanced] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const cardRef = useRef<HTMLDivElement>(null);
  useDismiss(cardRef, true, onClose);

  const meta = PROVIDER_META[form.provider_type];
  const canOAuth = OAUTH_CAPABLE.includes(form.provider_type);
  const isOAuth = canOAuth && form.auth_method === "oauth";

  function pickType(t: ProviderType) {
    setForm({
      ...empty,
      provider_type: t,
      name: PROVIDER_META[t].label,
      auth_method: "api_key",
    });
    setError("");
    setStep(2);
  }

  async function submit() {
    setError("");
    let extra: Record<string, unknown> = {};
    if (form.extra.trim()) {
      try {
        extra = JSON.parse(form.extra);
      } catch {
        setError("Advanced settings must be valid JSON.");
        return;
      }
    }
    if (!form.name.trim()) {
      setError("Please give the provider a display name.");
      return;
    }
    setSaving(true);
    try {
      const created = await create.mutateAsync({
        name: form.name.trim(),
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
      } as any);
      onCreated(created.id);
    } catch (e) {
      setError((e as Error).message || "Could not create the provider.");
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/40 p-4 sm:items-center">
      <div
        ref={cardRef}
        className="w-full max-w-2xl overflow-hidden rounded-xl border border-gray-200 bg-white shadow-2xl dark:border-gray-700 dark:bg-gray-900"
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-gray-200 px-5 py-3 dark:border-gray-700">
          <div>
            <h2 className="font-semibold">Add a provider</h2>
            <p className="text-xs text-gray-500">
              Step {step} of 2 · {step === 1 ? "Choose a provider" : meta?.label}
            </p>
          </div>
          <button
            onClick={onClose}
            className="rounded-full px-2 text-lg text-gray-400 hover:bg-gray-100 hover:text-gray-700 dark:hover:bg-gray-800"
            title="Close"
          >
            ✕
          </button>
        </div>

        {/* Step indicator */}
        <div className="flex gap-1 px-5 pt-3">
          {[1, 2].map((n) => (
            <div
              key={n}
              className={`h-1 flex-1 rounded-full ${
                step >= n ? "bg-brand" : "bg-gray-200 dark:bg-gray-700"
              }`}
            />
          ))}
        </div>

        {/* Body */}
        <div className="max-h-[65vh] overflow-y-auto px-5 py-4">
          {step === 1 ? (
            <div className="space-y-4">
              {GROUPS.map((group) => {
                const items = TYPES.filter(
                  (t) => PROVIDER_META[t].group === group,
                );
                if (items.length === 0) return null;
                return (
                  <div key={group}>
                    <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wide text-gray-400">
                      {group}
                    </div>
                    <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                      {items.map((t) => (
                        <button
                          key={t}
                          onClick={() => pickType(t)}
                          className="flex items-start gap-3 rounded-lg border border-gray-200 p-3 text-left transition hover:border-brand hover:bg-brand/5 dark:border-gray-700"
                        >
                          <ProviderIcon type={t} className="mt-0.5 h-6 w-6 shrink-0" />
                          <span className="min-w-0">
                            <span className="block text-sm font-medium">
                              {PROVIDER_META[t].label}
                            </span>
                            <span className="mt-0.5 block text-[11px] text-gray-500">
                              {PROVIDER_META[t].hint}
                            </span>
                          </span>
                        </button>
                      ))}
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="space-y-4">
              <div className="flex items-center gap-3 rounded-lg bg-gray-50 p-3 dark:bg-gray-800">
                <ProviderIcon type={form.provider_type} className="h-8 w-8 shrink-0" />
                <div className="min-w-0">
                  <div className="text-sm font-medium">{meta?.label}</div>
                  <div className="text-[11px] text-gray-500">{meta?.hint}</div>
                </div>
              </div>

              <div>
                <label className="mb-1 block text-xs font-medium text-gray-500">
                  Display name
                </label>
                <input
                  autoFocus
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                  placeholder={meta?.label}
                  autoComplete="off"
                  data-1p-ignore
                  data-lpignore="true"
                  className="w-full rounded-lg border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800"
                />
              </div>

              {canOAuth && (
                <div>
                  <label className="mb-1 block text-xs font-medium text-gray-500">
                    Authentication
                  </label>
                  <div className="flex gap-2">
                    {(["api_key", "oauth"] as AuthMethod[]).map((m) => (
                      <button
                        key={m}
                        onClick={() => setForm({ ...form, auth_method: m })}
                        className={`flex-1 rounded-lg border px-3 py-2 text-sm transition ${
                          form.auth_method === m
                            ? "border-brand bg-brand/10 font-medium text-brand"
                            : "border-gray-300 hover:bg-gray-50 dark:border-gray-600 dark:hover:bg-gray-800"
                        }`}
                      >
                        {m === "api_key" ? "🔑 API key" : "👤 OAuth sign-in"}
                      </button>
                    ))}
                  </div>
                  {isOAuth && (
                    <p className="mt-1.5 text-[11px] text-gray-500">
                      You'll sign in right after creating the provider.
                    </p>
                  )}
                </div>
              )}

              {needsBaseUrl(form.provider_type) && (
                <div>
                  <label className="mb-1 block text-xs font-medium text-gray-500">
                    Base URL
                  </label>
                  <input
                    value={form.base_url}
                    onChange={(e) => setForm({ ...form, base_url: e.target.value })}
                    placeholder={
                      form.provider_type === "ollama"
                        ? "http://localhost:11434"
                        : "https://…"
                    }
                    autoComplete="off"
                    data-1p-ignore
                    data-lpignore="true"
                    className="w-full rounded-lg border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800"
                  />
                </div>
              )}

              {!isOAuth && (
                <div>
                  <label className="mb-1 block text-xs font-medium text-gray-500">
                    API key{" "}
                    {form.provider_type === "ollama" && (
                      <span className="font-normal text-gray-400">(usually none)</span>
                    )}
                  </label>
                  <input
                    type="password"
                    value={form.api_key}
                    onChange={(e) => setForm({ ...form, api_key: e.target.value })}
                    placeholder="Paste your API key"
                    autoComplete="new-password"
                    data-1p-ignore
                    data-lpignore="true"
                    className="w-full rounded-lg border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800"
                  />
                </div>
              )}

              {/* Advanced */}
              <div>
                <button
                  onClick={() => setAdvanced((a) => !a)}
                  className="text-xs font-medium text-gray-500 hover:text-gray-700 dark:hover:text-gray-300"
                >
                  {advanced ? "▾" : "▸"} Advanced (models, extra JSON)
                </button>
                {advanced && (
                  <div className="mt-2 space-y-3 rounded-lg border border-gray-200 p-3 dark:border-gray-700">
                    <div>
                      <label className="mb-1 block text-xs font-medium text-gray-500">
                        Models (comma-separated)
                      </label>
                      <input
                        value={form.models}
                        onChange={(e) => setForm({ ...form, models: e.target.value })}
                        placeholder="gpt-5.5, gpt-4o…"
                        className="w-full rounded-lg border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800"
                      />
                      <p className="mt-1 text-[11px] text-gray-400">
                        Optional — you can fetch/refresh models after creating.
                      </p>
                    </div>
                    <div>
                      <label className="mb-1 block text-xs font-medium text-gray-500">
                        Default model
                      </label>
                      <input
                        value={form.default_model}
                        onChange={(e) =>
                          setForm({ ...form, default_model: e.target.value })
                        }
                        className="w-full rounded-lg border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800"
                      />
                    </div>
                    <div>
                      <label className="mb-1 block text-xs font-medium text-gray-500">
                        Extra (JSON)
                      </label>
                      <textarea
                        value={form.extra}
                        onChange={(e) => setForm({ ...form, extra: e.target.value })}
                        rows={3}
                        placeholder='{"api_version":"2024-08-01-preview","deployment":"gpt-4o"}'
                        className="w-full rounded-lg border border-gray-300 px-3 py-1.5 font-mono text-xs dark:border-gray-600 dark:bg-gray-800"
                      />
                    </div>
                  </div>
                )}
              </div>

              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={form.is_default}
                  onChange={(e) =>
                    setForm({ ...form, is_default: e.target.checked })
                  }
                />
                Set as the default provider
              </label>

              {error && <p className="text-xs text-red-500">⚠️ {error}</p>}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between border-t border-gray-200 px-5 py-3 dark:border-gray-700">
          {step === 2 ? (
            <button
              onClick={() => {
                setStep(1);
                setError("");
              }}
              className="rounded-lg border border-gray-300 px-3 py-1.5 text-sm hover:bg-gray-50 dark:border-gray-600 dark:hover:bg-gray-800"
            >
              ← Back
            </button>
          ) : (
            <span />
          )}
          {step === 2 && (
            <button
              onClick={submit}
              disabled={saving}
              className="rounded-lg bg-brand px-4 py-1.5 text-sm font-medium text-white hover:brightness-110 disabled:opacity-50"
            >
              {saving ? "Creating…" : "Create provider"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
