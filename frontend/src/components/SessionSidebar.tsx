import { useEffect, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { asUtcDate } from "../api/client";
import type { Persona, SearchHit, SessionListItem } from "../api/types";
import { searchSessions, useFolderMutations, useFolders } from "../hooks/useExtras";
import { useDismiss } from "../hooks/useDismiss";
import { useSessionMutations } from "../hooks/useSessions";

interface Props {
  sessions: SessionListItem[];
  personas: Persona[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onNew: (persona?: Persona) => void;
  onRename: (id: string, title: string) => void;
  onDelete: (id: string) => void;
  onCollapse: () => void;
}

function relTime(iso: string): string {
  const d = asUtcDate(iso).getTime();
  const diff = Date.now() - d;
  const m = Math.floor(diff / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

export function SessionSidebar({
  sessions,
  personas,
  activeId,
  onSelect,
  onNew,
  onRename,
  onDelete,
  onCollapse,
}: Props) {
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [menuOpen, setMenuOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchHit[]>([]);
  const [showArchived, setShowArchived] = useState(false);
  const [showTrash, setShowTrash] = useState(false);
  // Keep the Settings section expanded whenever we're on any settings-related route, so it
  // doesn't collapse when navigating between settings pages (each renders a fresh sidebar).
  const location = useLocation();
  const onSettingsRoute =
    /^\/(settings|personas|snippets|analytics|evals|integrations)(\/|$)/.test(
      location.pathname,
    );
  const [settingsOpen, setSettingsOpen] = useState(onSettingsRoute);
  // If navigation lands on a settings route while this sidebar stays mounted, expand it.
  useEffect(() => {
    if (onSettingsRoute) setSettingsOpen(true);
  }, [onSettingsRoute]);
  const { data: folders = [] } = useFolders();
  const folderMut = useFolderMutations();
  const sm = useSessionMutations();
  const newMenuRef = useRef<HTMLDivElement>(null);
  useDismiss(newMenuRef, menuOpen, () => setMenuOpen(false));

  // Debounced search.
  useEffect(() => {
    const q = query.trim();
    if (!q) {
      setResults([]);
      return;
    }
    const t = setTimeout(() => {
      searchSessions(q).then(setResults).catch(() => setResults([]));
    }, 250);
    return () => clearTimeout(t);
  }, [query]);

  const patch = (id: string, body: Record<string, unknown>) =>
    sm.update.mutate({ id, body });

  return (
    <div className="flex h-full w-60 shrink-0 flex-col border-r border-gray-200 bg-gray-50 dark:border-gray-700 dark:bg-gray-950">
      <div className="flex items-center justify-between px-2 pt-2">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-gray-400">
          MultiChat
        </span>
        <button
          onClick={onCollapse}
          title="Collapse sidebar"
          className="rounded px-1 text-gray-400 hover:bg-gray-200 dark:hover:bg-gray-800"
        >
          ⏴
        </button>
      </div>
      <div className="relative p-2 pt-1" ref={newMenuRef}>
        <div className="flex gap-1">
          <button
            onClick={() => setMenuOpen((o) => !o)}
            className="flex-1 rounded bg-brand px-3 py-1.5 text-xs font-medium text-white hover:brightness-110"
          >
            ✏️ New chat
          </button>
          <button
            onClick={() => setMenuOpen((o) => !o)}
            title="Start from a persona"
            className="rounded bg-brand px-2 py-1.5 text-xs font-medium text-white hover:brightness-110"
          >
            ▾
          </button>
        </div>
        {menuOpen && (
          <div className="absolute left-2 right-2 z-10 mt-1 rounded-lg border border-gray-200 bg-white py-1 shadow-lg dark:border-gray-700 dark:bg-gray-900">
            <button
              onClick={() => {
                setMenuOpen(false);
                onNew();
              }}
              className="block w-full px-3 py-1.5 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-800"
            >
              Blank topic
            </button>
            {personas.length > 0 && (
              <div className="mt-1 border-t border-gray-100 pt-1 dark:border-gray-800">
                <div className="px-3 pb-0.5 text-[10px] font-semibold uppercase tracking-wide text-gray-400">
                  Personas
                </div>
                {personas.map((p) => (
                  <button
                    key={p.id}
                    onClick={() => {
                      setMenuOpen(false);
                      onNew(p);
                    }}
                    className="block w-full px-3 py-1.5 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-800"
                    title={p.description || ""}
                  >
                    <span className="block truncate">{p.name}</span>
                    <span className="block truncate text-[11px] text-gray-400">
                      {p.lanes.length} lane{p.lanes.length === 1 ? "" : "s"}
                      {p.lanes.length > 0 &&
                        ` · ${p.lanes.map((l) => l.model).slice(0, 2).join(", ")}`}
                    </span>
                  </button>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Settings (expandable) */}
        <div className="mt-2">
          <button
            onClick={() => setSettingsOpen((o) => !o)}
            className="flex w-full items-center justify-between rounded px-2 py-1 text-xs font-medium text-gray-600 hover:bg-gray-200 dark:text-gray-300 dark:hover:bg-gray-800"
          >
            <span>⚙️ Settings</span>
            <span className="text-gray-400">{settingsOpen ? "▾" : "▸"}</span>
          </button>
          {settingsOpen && (
            <div className="ml-2 mt-0.5 space-y-0.5 border-l border-gray-200 pl-2 dark:border-gray-700">
              <div className="px-2 pb-0.5 pt-1 text-[10px] font-semibold uppercase tracking-wide text-gray-400">
                Configuration
              </div>
              <Link
                to="/settings"
                className="block rounded px-2 py-1 text-xs text-gray-600 hover:bg-gray-200 dark:text-gray-300 dark:hover:bg-gray-800"
              >
                🔌 AI Providers
              </Link>
              <Link
                to="/personas"
                className="block rounded px-2 py-1 text-xs text-gray-600 hover:bg-gray-200 dark:text-gray-300 dark:hover:bg-gray-800"
              >
                🎭 Personas
              </Link>
              <Link
                to="/snippets"
                className="block rounded px-2 py-1 text-xs text-gray-600 hover:bg-gray-200 dark:text-gray-300 dark:hover:bg-gray-800"
              >
                ⚡ Snippets
              </Link>
              <Link
                to="/analytics"
                className="block rounded px-2 py-1 text-xs text-gray-600 hover:bg-gray-200 dark:text-gray-300 dark:hover:bg-gray-800"
              >
                📊 Usage analytics
              </Link>
              <Link
                to="/evals"
                className="block rounded px-2 py-1 text-xs text-gray-600 hover:bg-gray-200 dark:text-gray-300 dark:hover:bg-gray-800"
              >
                🧪 Evaluations
              </Link>
              <Link
                to="/integrations"
                className="block rounded px-2 py-1 text-xs text-gray-600 hover:bg-gray-200 dark:text-gray-300 dark:hover:bg-gray-800"
              >
                🔗 Integrations
              </Link>
              <Link
                to="/settings/general"
                className="block rounded px-2 py-1 text-xs text-gray-600 hover:bg-gray-200 dark:text-gray-300 dark:hover:bg-gray-800"
              >
                🛠 General
              </Link>
            </div>
          )}
        </div>
      </div>
      <div className="px-2 pb-1">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="🔍 Search chats…"
          className="w-full rounded border border-gray-300 px-2 py-1 text-xs dark:border-gray-600 dark:bg-gray-800"
        />
      </div>
      <div className="flex-1 overflow-y-auto">
        {query.trim() ? (
          <div>
            {results.length === 0 ? (
              <div className="p-3 text-xs text-gray-500">No matches.</div>
            ) : (
              results.map((r) => (
                <button
                  key={r.session_id}
                  onClick={() => {
                    onSelect(r.session_id);
                    setQuery("");
                  }}
                  className="block w-full px-3 py-2 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-800"
                >
                  <div className="truncate font-medium">{r.title}</div>
                  {r.snippet && (
                    <div className="truncate text-xs text-gray-400">{r.snippet}</div>
                  )}
                </button>
              ))
            )}
          </div>
        ) : (
          <>
            {renderGroup(
              "Pinned",
              sessions.filter((s) => s.pinned && !s.archived && !s.trashed)
            )}
            {folders.map((f) => {
              const items = sessions.filter(
                (s) => s.folder_id === f.id && !s.archived && !s.pinned && !s.trashed
              );
              return (
                <div key={f.id}>
                  <div className="group flex items-center justify-between px-3 pt-2 text-[10px] font-semibold uppercase tracking-wide text-gray-400">
                    <span>📁 {f.name}</span>
                    <button
                      onClick={() => {
                        if (confirm(`Delete project "${f.name}"? (chats are kept)`))
                          folderMut.remove.mutate(f.id);
                      }}
                      className="opacity-0 group-hover:opacity-100"
                    >
                      ×
                    </button>
                  </div>
                  {items.length === 0 ? (
                    <div className="px-3 py-1 text-[11px] text-gray-400">empty</div>
                  ) : (
                    items.map((s) => renderRow(s))
                  )}
                </div>
              );
            })}
            {renderGroup(
              folders.length || sessions.some((s) => s.pinned) ? "Chats" : "",
              sessions.filter((s) => !s.folder_id && !s.archived && !s.pinned && !s.trashed)
            )}
            {sessions.some((s) => s.archived && !s.trashed) && (
              <div>
                <button
                  onClick={() => setShowArchived((v) => !v)}
                  className="w-full px-3 pt-2 text-left text-[10px] font-semibold uppercase tracking-wide text-gray-400 hover:text-gray-600"
                >
                  🗄 Archived ({sessions.filter((s) => s.archived && !s.trashed).length}){" "}
                  {showArchived ? "▾" : "▸"}
                </button>
                {showArchived &&
                  sessions.filter((s) => s.archived && !s.trashed).map((s) => renderRow(s))}
              </div>
            )}
            {sessions.some((s) => s.trashed) && (
              <div>
                <div className="flex items-center justify-between px-3 pt-2">
                  <button
                    onClick={() => setShowTrash((v) => !v)}
                    className="text-left text-[10px] font-semibold uppercase tracking-wide text-gray-400 hover:text-gray-600"
                  >
                    🗑 Trash ({sessions.filter((s) => s.trashed).length}){" "}
                    {showTrash ? "▾" : "▸"}
                  </button>
                  <button
                    onClick={() => {
                      const count = sessions.filter((s) => s.trashed).length;
                      if (
                        confirm(
                          `Permanently delete all ${count} item(s) in Trash? This cannot be undone.`
                        )
                      )
                        sm.emptyTrash.mutate();
                    }}
                    disabled={sm.emptyTrash.isPending}
                    title="Permanently delete everything in Trash"
                    className="text-[10px] font-medium text-gray-400 hover:text-red-500 disabled:opacity-50"
                  >
                    {sm.emptyTrash.isPending ? "Emptying…" : "Empty"}
                  </button>
                </div>
                {showTrash &&
                  sessions.filter((s) => s.trashed).map((s) => renderRow(s, true))}
              </div>
            )}
            <div className="p-2">
              <button
                onClick={() => folderMut.create.mutate("New project")}
                className="w-full rounded border border-dashed border-gray-300 px-2 py-1 text-xs text-gray-500 hover:bg-gray-100 dark:border-gray-700 dark:hover:bg-gray-800"
              >
                + New project
              </button>
            </div>
            {sessions.length === 0 && (
              <div className="p-3 text-xs text-gray-500">No topics yet.</div>
            )}
          </>
        )}
      </div>
    </div>
  );

  function renderGroup(label: string, items: SessionListItem[]) {
    if (items.length === 0) return null;
    return (
      <div>
        {label && (
          <div className="px-3 pt-2 text-[10px] font-semibold uppercase tracking-wide text-gray-400">
            {label}
          </div>
        )}
        {items.map((s) => renderRow(s))}
      </div>
    );
  }

  function renderRow(s: SessionListItem, inTrash = false) {
    return (
      <div
        key={s.id}
        className={`group flex items-center gap-1 px-2 py-2 text-sm ${
          s.id === activeId
            ? "bg-brand/10"
            : "hover:bg-gray-100 dark:hover:bg-gray-800"
        }`}
      >
        {editing === s.id ? (
          <input
            autoFocus
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={() => {
              onRename(s.id, draft || s.title);
              setEditing(null);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                onRename(s.id, draft || s.title);
                setEditing(null);
              }
            }}
            className="flex-1 rounded border px-1 text-sm dark:bg-gray-800"
          />
        ) : (
          <button
            onClick={() => onSelect(s.id)}
            onDoubleClick={() => {
              setEditing(s.id);
              setDraft(s.title);
            }}
            className={`min-w-0 flex-1 text-left ${s.id === activeId ? "text-brand" : "text-gray-700 dark:text-gray-200"}`}
          >
            <div className="truncate font-medium">
              {s.pinned && "📌 "}
              {s.title}
            </div>
            <div className="truncate text-xs text-gray-500 dark:text-gray-400">
              {relTime(s.updated_at)} · {s.lane_count} lanes · {s.message_count}{" "}
              msg{s.message_count === 1 ? "" : "s"}
            </div>
          </button>
        )}
        <div className="hidden shrink-0 items-center group-hover:flex">
          {inTrash ? (
            <>
              <button
                onClick={() => patch(s.id, { trashed: false })}
                title="Restore"
                className="px-1 text-xs hover:text-green-600"
              >
                ♻
              </button>
              <button
                onClick={() => {
                  if (confirm(`Permanently delete "${s.title}"? This cannot be undone.`))
                    onDelete(s.id);
                }}
                title="Delete permanently"
                className="px-1 text-xs hover:text-red-500"
              >
                ✕
              </button>
            </>
          ) : (
            <>
              <button
                onClick={() => {
                  setEditing(s.id);
                  setDraft(s.title);
                }}
                title="Rename"
                className="px-1 text-xs"
              >
                ✎
              </button>
              <button
                onClick={() => patch(s.id, { pinned: !s.pinned })}
                title={s.pinned ? "Unpin" : "Pin"}
                className="px-1 text-xs"
              >
                {s.pinned ? "📌" : "📍"}
              </button>
              <button
                onClick={() => patch(s.id, { archived: !s.archived })}
                title={s.archived ? "Unarchive" : "Archive"}
                className="px-1 text-xs"
              >
                🗄
              </button>
              <button
                onClick={() => {
                  // Deleting (trashing) the currently-open chat: auto-focus the next
                  // available chat — prefer the one just below, else the one just above.
                  if (s.id === activeId) {
                    const idx = sessions.findIndex((x) => x.id === s.id);
                    const pool = sessions.filter(
                      (x) => !x.trashed && x.id !== s.id
                    );
                    const next =
                      pool.find((x) => sessions.indexOf(x) > idx) ||
                      [...pool].reverse().find((x) => sessions.indexOf(x) < idx) ||
                      pool[0] ||
                      null;
                    if (next) onSelect(next.id);
                  }
                  patch(s.id, { trashed: true });
                }}
                title="Move to trash"
                className="px-1 text-xs hover:text-red-500"
              >
                🗑
              </button>
            </>
          )}
        </div>
      </div>
    );
  }
}
