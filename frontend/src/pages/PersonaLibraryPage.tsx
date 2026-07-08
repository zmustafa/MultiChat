import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import type { Persona, PersonaLane, Provider } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import {
  usePersonaEnhance,
  usePersonaMutations,
  usePersonas,
} from "../hooks/usePersonas";
import { useProviders } from "../hooks/useProviders";
import { useSessionMutations } from "../hooks/useSessions";
import { seedLaneCollapse } from "../utils/laneCollapse";
import { ModelPicker } from "../components/ModelPicker";
import { ThemeToggle } from "../components/ThemeToggle";

function providerName(providers: Provider[], id: string): string {
  return providers.find((p) => p.id === id)?.name || "unknown";
}

function PersonaEditor({
  persona,
  providers,
  onClose,
}: {
  persona: Persona | "new";
  providers: Provider[];
  onClose: () => void;
}) {
  const isNew = persona === "new";
  const p = isNew ? null : persona;
  const [name, setName] = useState(p?.name || "New persona");
  const [description, setDescription] = useState(p?.description || "");
  const [systemPrompt, setSystemPrompt] = useState(p?.system_prompt || "");
  const [toolsEnabled, setToolsEnabled] = useState(p?.tools_enabled ?? true);
  const [lanes, setLanes] = useState<PersonaLane[]>(p?.lanes || []);
  const [instruction, setInstruction] = useState("");
  const [enhanceNote, setEnhanceNote] = useState<string | null>(null);

  const { create, update } = usePersonaMutations();
  const enhance = usePersonaEnhance();

  async function runEnhance(mode: "enhance" | "generate") {
    setEnhanceNote(null);
    try {
      const res = await enhance.mutateAsync({
        mode,
        name,
        description: description || null,
        system_prompt: systemPrompt || null,
        instruction: instruction || null,
      });
      setSystemPrompt(res.system_prompt);
      setEnhanceNote(`✨ ${mode === "generate" ? "Generated" : "Enhanced"} with ${res.used_provider} · ${res.used_model}`);
    } catch (e) {
      setEnhanceNote(`⚠️ ${(e as Error).message}`);
    }
  }

  function save() {
    const body = {
      name: name.trim() || "Untitled persona",
      description: description || null,
      system_prompt: systemPrompt || null,
      tools_enabled: toolsEnabled,
      lanes,
    };
    if (isNew) create.mutate(body, { onSuccess: onClose });
    else update.mutate({ id: p!.id, body }, { onSuccess: onClose });
  }

  return (
    <div className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-black/40 p-4">
      <div className="mt-8 w-full max-w-3xl rounded-xl border border-gray-200 bg-white shadow-2xl dark:border-gray-700 dark:bg-gray-900">
        <div className="flex items-center justify-between border-b border-gray-200 px-5 py-3 dark:border-gray-700">
          <h2 className="font-semibold">
            {isNew ? "New persona" : `Edit — ${p?.name}`}
          </h2>
          <button
            onClick={onClose}
            className="rounded px-2 py-1 text-sm text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800"
          >
            ✕
          </button>
        </div>

        <div className="space-y-4 p-5">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              <label className="mb-1 block text-xs font-medium text-gray-500">Name</label>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                className="w-full rounded-lg border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800"
              />
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-gray-500">
                Description
              </label>
              <input
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="short blurb"
                className="w-full rounded-lg border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800"
              />
            </div>
          </div>

          <div>
            <div className="mb-1 flex items-center justify-between">
              <label className="text-xs font-medium text-gray-500">System prompt</label>
              <div className="flex items-center gap-1">
                <button
                  onClick={() => runEnhance("enhance")}
                  disabled={enhance.isPending || !systemPrompt.trim()}
                  title="Improve the current system prompt with AI (default model)"
                  className="rounded border border-purple-300 px-2 py-0.5 text-xs text-purple-600 hover:bg-purple-50 disabled:opacity-40 dark:border-purple-800 dark:hover:bg-purple-950/40"
                >
                  {enhance.isPending ? "…" : "✨ Enhance"}
                </button>
                <button
                  onClick={() => runEnhance("generate")}
                  disabled={enhance.isPending || (!name.trim() && !description.trim())}
                  title="Generate a system prompt from the name/description with AI"
                  className="rounded border border-purple-300 px-2 py-0.5 text-xs text-purple-600 hover:bg-purple-50 disabled:opacity-40 dark:border-purple-800 dark:hover:bg-purple-950/40"
                >
                  ✨ Generate
                </button>
              </div>
            </div>
            <textarea
              value={systemPrompt}
              onChange={(e) => setSystemPrompt(e.target.value)}
              rows={6}
              placeholder="You are a senior Cloud Architect…"
              className="w-full rounded-lg border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800"
            />
            <input
              value={instruction}
              onChange={(e) => setInstruction(e.target.value)}
              placeholder="Optional: guidance for AI enhance (e.g. 'make it concise, add safety rules')"
              className="mt-1 w-full rounded-lg border border-gray-200 px-3 py-1 text-xs dark:border-gray-700 dark:bg-gray-800"
            />
            {enhanceNote && (
              <div className="mt-1 text-xs text-gray-500">{enhanceNote}</div>
            )}
          </div>

          <div>
            <label className="mb-1 block text-xs font-medium text-gray-500">
              Lanes ({lanes.length})
            </label>
            <div className="mb-2 flex flex-wrap gap-1">
              {lanes.map((l, i) => (
                <span
                  key={i}
                  className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs ${
                    l.collapsed
                      ? "bg-amber-50 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300"
                      : "bg-gray-100 dark:bg-gray-800"
                  }`}
                >
                  <button
                    onClick={() =>
                      setLanes((prev) => {
                        if (i === 0) return prev;
                        const next = [...prev];
                        [next[i - 1], next[i]] = [next[i], next[i - 1]];
                        return next;
                      })
                    }
                    disabled={i === 0}
                    title="Move left"
                    className="text-gray-400 hover:text-gray-700 disabled:opacity-30 dark:hover:text-gray-200"
                  >
                    ◀
                  </button>
                  {l.role === "judge" && "⚖️ "}
                  {l.model}
                  <span className="text-gray-400">
                    · {providerName(providers, l.provider_id)}
                  </span>
                  <button
                    onClick={() =>
                      setLanes((prev) => {
                        if (i === prev.length - 1) return prev;
                        const next = [...prev];
                        [next[i + 1], next[i]] = [next[i], next[i + 1]];
                        return next;
                      })
                    }
                    disabled={i === lanes.length - 1}
                    title="Move right"
                    className="text-gray-400 hover:text-gray-700 disabled:opacity-30 dark:hover:text-gray-200"
                  >
                    ▶
                  </button>
                  <button
                    onClick={() =>
                      setLanes((prev) =>
                        prev.map((x, j) =>
                          j === i ? { ...x, collapsed: !x.collapsed } : x
                        )
                      )
                    }
                    title={
                      l.collapsed
                        ? "Starts minimized — click to start expanded"
                        : "Starts expanded — click to start minimized"
                    }
                    className={
                      l.collapsed
                        ? "text-amber-600 dark:text-amber-400"
                        : "text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
                    }
                  >
                    {l.collapsed ? "🗕" : "🗖"}
                  </button>
                  <button
                    onClick={() => setLanes((prev) => prev.filter((_, j) => j !== i))}
                    className="text-gray-400 hover:text-red-500"
                  >
                    ×
                  </button>
                </span>
              ))}
              {lanes.length === 0 && (
                <span className="text-xs text-gray-400">
                  No lanes yet — add some below.
                </span>
              )}
            </div>
            {lanes.length > 0 && (
              <p className="mb-2 text-[10px] text-gray-400">
                ◀ ▶ reorder how lanes appear · 🗕 lane starts minimized when the chat
                launches (expand it anytime)
              </p>
            )}
            {providers.length > 0 && (
              <ModelPicker
                providers={providers}
                allowJudge
                onAdd={(provider_id, model, role) =>
                  setLanes((prev) => [...prev, { provider_id, model, role }])
                }
              />
            )}
          </div>

          <label className="flex items-center gap-1 text-xs text-gray-600 dark:text-gray-300">
            <input
              type="checkbox"
              checked={toolsEnabled}
              onChange={(e) => setToolsEnabled(e.target.checked)}
            />
            Tools on by default
          </label>
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-gray-200 px-5 py-3 dark:border-gray-700">
          <button
            onClick={onClose}
            className="rounded-lg border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-600"
          >
            Cancel
          </button>
          <button
            onClick={save}
            className="rounded-lg bg-brand px-4 py-1.5 text-sm font-medium text-white hover:brightness-110"
          >
            Save persona
          </button>
        </div>
      </div>
    </div>
  );
}

export function PersonaLibraryPage() {
  const { logout, user } = useAuth();
  const nav = useNavigate();
  const { data: personas = [] } = usePersonas();
  const { data: providers = [] } = useProviders();
  const { create, remove, setDefault } = usePersonaMutations();
  const sm = useSessionMutations();
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState<Persona | "new" | null>(null);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return personas;
    return personas.filter((p) =>
      `${p.name} ${p.description || ""} ${p.system_prompt || ""}`
        .toLowerCase()
        .includes(q)
    );
  }, [personas, query]);

  async function startTopic(p: Persona) {
    const created = await sm.create.mutateAsync({
      title: p.name,
      system_prompt: p.system_prompt || undefined,
      tools_enabled: p.tools_enabled,
      lanes: p.lanes.map((l) => ({
        provider_id: l.provider_id,
        model: l.model,
        role: l.role,
      })),
    });
    seedLaneCollapse(
      created.lanes.map((l) => l.id),
      p.lanes.map((l) => !!l.collapsed)
    );
    nav(`/c/${created.id}`);
  }

  function clonePersona(p: Persona) {
    create.mutate({
      name: `${p.name} (copy)`,
      description: p.description,
      system_prompt: p.system_prompt,
      tools_enabled: p.tools_enabled,
      lanes: p.lanes,
    });
  }

  function exportPersona(p: Persona) {
    const data = {
      name: p.name,
      description: p.description,
      system_prompt: p.system_prompt,
      tools_enabled: p.tools_enabled,
      lanes: p.lanes,
    };
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(data, null, 2)], { type: "application/json" })
    );
    const a = document.createElement("a");
    a.href = url;
    a.download = `persona-${p.name.replace(/\s+/g, "-").toLowerCase()}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="flex h-full flex-col bg-gray-50 dark:bg-gray-950">
      <header className="flex items-center justify-between border-b border-gray-200 bg-white px-4 py-2 dark:border-gray-700 dark:bg-gray-900">
        <div className="flex items-center gap-3">
          <Link to="/" className="text-sm text-blue-500">
            ← Chat
          </Link>
          <h1 className="text-lg font-semibold">Persona Library</h1>
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

      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-6xl p-5">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
            <div>
              <h2 className="font-medium">Your personas</h2>
              <p className="mt-0.5 text-xs text-gray-500">
                Reusable topic templates — a system prompt plus provider/model lanes.
                Clone, AI-enhance, export, and launch a chat from any persona.
              </p>
            </div>
            <button
              onClick={() => setEditing("new")}
              className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white hover:brightness-110"
            >
              + New persona
            </button>
          </div>

          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="🔍 Search personas…"
            className="mb-4 w-full max-w-sm rounded-lg border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800"
          />

          {filtered.length === 0 ? (
            <div className="rounded-lg border border-dashed border-gray-300 p-10 text-center text-sm text-gray-500 dark:border-gray-700">
              {personas.length === 0
                ? "No personas yet. Create one, or use “★ Save persona” on a topic."
                : "No personas match your search."}
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {filtered.map((p) => (
                <div
                  key={p.id}
                  className="flex flex-col rounded-xl border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-gray-900"
                >
                  <div className="mb-1 flex items-start justify-between gap-2">
                    <h3 className="truncate font-semibold" title={p.name}>
                      {p.name}
                    </h3>
                    <div className="flex shrink-0 items-center gap-1">
                      {p.is_default && (
                        <span
                          title="Opens automatically on New chat"
                          className="rounded bg-brand/15 px-1.5 py-0.5 text-[10px] font-medium text-brand"
                        >
                          ★ default
                        </span>
                      )}
                      {p.tools_enabled && (
                        <span
                          title="Tools on by default"
                          className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] text-amber-700 dark:bg-amber-950/50 dark:text-amber-400"
                        >
                          🛠 tools
                        </span>
                      )}
                    </div>
                  </div>
                  {p.description && (
                    <p className="mb-2 text-xs text-gray-500">{p.description}</p>
                  )}
                  {p.system_prompt && (
                    <p className="mb-2 line-clamp-3 text-xs text-gray-400">
                      {p.system_prompt}
                    </p>
                  )}
                  <div className="mb-3 flex flex-wrap gap-1">
                    {p.lanes.map((l, i) => (
                      <span
                        key={i}
                        className="inline-flex items-center gap-1 rounded-full bg-gray-100 px-2 py-0.5 text-[10px] dark:bg-gray-800"
                      >
                        {l.role === "judge" && "⚖️ "}
                        {l.model}
                        <span className="text-gray-400">
                          · {providerName(providers, l.provider_id)}
                        </span>
                      </span>
                    ))}
                    {p.lanes.length === 0 && (
                      <span className="text-[10px] text-gray-400">no lanes</span>
                    )}
                  </div>
                  <div className="mt-auto flex flex-wrap items-center gap-1 border-t border-gray-100 pt-2 text-xs dark:border-gray-800">
                    <button
                      onClick={() => startTopic(p)}
                      className="rounded bg-brand px-2 py-1 font-medium text-white hover:brightness-110"
                    >
                      ▶ Use
                    </button>
                    <button
                      onClick={() =>
                        setDefault.mutate({ id: p.id, isDefault: !p.is_default })
                      }
                      title={
                        p.is_default
                          ? "This persona opens on New chat — click to unset"
                          : "Make this the default persona for New chat"
                      }
                      className={`rounded border px-2 py-1 ${
                        p.is_default
                          ? "border-brand bg-brand/10 text-brand"
                          : "border-gray-300 hover:bg-gray-50 dark:border-gray-600 dark:hover:bg-gray-800"
                      }`}
                    >
                      {p.is_default ? "★ Default" : "☆ Default"}
                    </button>
                    <button
                      onClick={() => setEditing(p)}
                      className="rounded border border-gray-300 px-2 py-1 hover:bg-gray-50 dark:border-gray-600 dark:hover:bg-gray-800"
                    >
                      Edit
                    </button>
                    <button
                      onClick={() => clonePersona(p)}
                      title="Duplicate this persona"
                      className="rounded border border-gray-300 px-2 py-1 hover:bg-gray-50 dark:border-gray-600 dark:hover:bg-gray-800"
                    >
                      ⧉ Clone
                    </button>
                    <button
                      onClick={() => exportPersona(p)}
                      title="Export as JSON"
                      className="rounded border border-gray-300 px-2 py-1 hover:bg-gray-50 dark:border-gray-600 dark:hover:bg-gray-800"
                    >
                      ⭳
                    </button>
                    <button
                      onClick={() => {
                        if (confirm(`Delete persona “${p.name}”?`)) remove.mutate(p.id);
                      }}
                      className="ml-auto rounded border border-red-200 px-2 py-1 text-red-600 hover:bg-red-50 dark:border-red-900 dark:hover:bg-red-950/40"
                    >
                      🗑
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {editing && (
        <PersonaEditor
          persona={editing}
          providers={providers}
          onClose={() => setEditing(null)}
        />
      )}
    </div>
  );
}
