import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import type { Snippet } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { useSnippetMutations, useSnippets } from "../hooks/useExtras";
import { ThemeToggle } from "../components/ThemeToggle";

function SnippetEditor({
  snippet,
  onClose,
}: {
  snippet: Snippet | "new";
  onClose: () => void;
}) {
  const isNew = snippet === "new";
  const s = isNew ? null : snippet;
  const [title, setTitle] = useState(s?.title || "New snippet");
  const [content, setContent] = useState(s?.content || "");
  const { create, update } = useSnippetMutations();

  function save() {
    const body = { title: title.trim() || "Untitled", content };
    if (isNew) create.mutate(body, { onSuccess: onClose });
    else update.mutate({ id: s!.id, body }, { onSuccess: onClose });
  }

  return (
    <div className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-black/40 p-4">
      <div className="mt-8 w-full max-w-2xl rounded-xl border border-gray-200 bg-white shadow-2xl dark:border-gray-700 dark:bg-gray-900">
        <div className="flex items-center justify-between border-b border-gray-200 px-5 py-3 dark:border-gray-700">
          <h2 className="font-semibold">{isNew ? "New snippet" : `Edit — ${s?.title}`}</h2>
          <button
            onClick={onClose}
            className="rounded px-2 py-1 text-sm text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800"
          >
            ✕
          </button>
        </div>
        <div className="space-y-3 p-5">
          <div>
            <label className="mb-1 block text-xs font-medium text-gray-500">Title</label>
            <input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              className="w-full rounded-lg border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800"
            />
          </div>
          <div>
            <label className="mb-1 block text-xs font-medium text-gray-500">Content</label>
            <textarea
              value={content}
              onChange={(e) => setContent(e.target.value)}
              rows={8}
              placeholder="The reusable prompt text…"
              className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800"
            />
            <div className="mt-1 text-right text-[11px] text-gray-400">
              {content.length} chars
            </div>
          </div>
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
            Save snippet
          </button>
        </div>
      </div>
    </div>
  );
}

export function SnippetLibraryPage() {
  const { logout, user } = useAuth();
  const { data: snippets = [] } = useSnippets();
  const { create, remove } = useSnippetMutations();
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState<Snippet | "new" | null>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return snippets;
    return snippets.filter((s) =>
      `${s.title} ${s.content}`.toLowerCase().includes(q)
    );
  }, [snippets, query]);

  function cloneSnippet(s: Snippet) {
    create.mutate({ title: `${s.title} (copy)`, content: s.content });
  }

  function copyContent(s: Snippet) {
    navigator.clipboard.writeText(s.content);
    setCopiedId(s.id);
    setTimeout(() => setCopiedId(null), 1200);
  }

  return (
    <div className="flex h-full flex-col bg-gray-50 dark:bg-gray-950">
      <header className="flex items-center justify-between border-b border-gray-200 bg-white px-4 py-2 dark:border-gray-700 dark:bg-gray-900">
        <div className="flex items-center gap-3">
          <Link to="/" className="text-sm text-blue-500">
            ← Chat
          </Link>
          <h1 className="text-lg font-semibold">Snippet Library</h1>
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
              <h2 className="font-medium">Your snippets</h2>
              <p className="mt-0.5 text-xs text-gray-500">
                Reusable prompt templates — insert them in the composer with the ⚡
                button. Create, clone, copy, and manage them here.
              </p>
            </div>
            <button
              onClick={() => setEditing("new")}
              className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white hover:brightness-110"
            >
              + New snippet
            </button>
          </div>

          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="🔍 Search snippets…"
            className="mb-4 w-full max-w-sm rounded-lg border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800"
          />

          {filtered.length === 0 ? (
            <div className="rounded-lg border border-dashed border-gray-300 p-10 text-center text-sm text-gray-500 dark:border-gray-700">
              {snippets.length === 0
                ? "No snippets yet. Create one to reuse in the composer."
                : "No snippets match your search."}
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {filtered.map((s) => (
                <div
                  key={s.id}
                  className="flex flex-col rounded-xl border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-gray-900"
                >
                  <h3 className="mb-1 truncate font-semibold" title={s.title}>
                    {s.title}
                  </h3>
                  <p className="mb-3 line-clamp-4 whitespace-pre-wrap text-xs text-gray-500">
                    {s.content || <span className="italic text-gray-400">empty</span>}
                  </p>
                  <div className="mt-auto flex flex-wrap items-center gap-1 border-t border-gray-100 pt-2 text-xs dark:border-gray-800">
                    <button
                      onClick={() => setEditing(s)}
                      className="rounded border border-gray-300 px-2 py-1 hover:bg-gray-50 dark:border-gray-600 dark:hover:bg-gray-800"
                    >
                      Edit
                    </button>
                    <button
                      onClick={() => copyContent(s)}
                      className="rounded border border-gray-300 px-2 py-1 hover:bg-gray-50 dark:border-gray-600 dark:hover:bg-gray-800"
                    >
                      {copiedId === s.id ? "✓ Copied" : "Copy"}
                    </button>
                    <button
                      onClick={() => cloneSnippet(s)}
                      title="Duplicate this snippet"
                      className="rounded border border-gray-300 px-2 py-1 hover:bg-gray-50 dark:border-gray-600 dark:hover:bg-gray-800"
                    >
                      ⧉ Clone
                    </button>
                    <button
                      onClick={() => {
                        if (confirm(`Delete snippet “${s.title}”?`)) remove.mutate(s.id);
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
        <SnippetEditor snippet={editing} onClose={() => setEditing(null)} />
      )}
    </div>
  );
}
