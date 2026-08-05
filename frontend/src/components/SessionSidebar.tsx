import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link, useLocation, useNavigate } from "react-router";
import { asUtcDate } from "../api/client";
import type { Persona, SearchHit, SessionListItem } from "../api/types";
import { searchSessions, useFolderMutations, useFolders, useUserSettings } from "../hooks/useExtras";
import { useDismiss } from "../hooks/useDismiss";
import { useSessionMutations } from "../hooks/useSessions";

interface Props {
  sessions: SessionListItem[];
  personas: Persona[];
  activeId: string | null;
  generatingIds?: Set<string>;
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

/** Ultra-short age for the single-line row ("now", "5m", "3h", "2d", "4 Mar"). */
function shortAge(iso: string): string {
  const d = asUtcDate(iso);
  const m = Math.floor((Date.now() - d.getTime()) / 60000);
  if (m < 1) return "now";
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h`;
  const days = Math.floor(h / 24);
  if (days < 7) return `${days}d`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** Which "Today / Yesterday / …" section a chat belongs to, by last activity. */
const DATE_BUCKETS = [
  "Today",
  "Yesterday",
  "Previous 7 days",
  "Previous 30 days",
  "Older",
] as const;

function dateBucket(iso: string): (typeof DATE_BUCKETS)[number] {
  const d = asUtcDate(iso);
  const startOfToday = new Date();
  startOfToday.setHours(0, 0, 0, 0);
  const daysAgo = Math.floor(
    (startOfToday.getTime() - d.getTime()) / 86400000,
  );
  if (daysAgo < 0) return "Today";
  if (daysAgo < 1) return "Yesterday";
  if (daysAgo < 7) return "Previous 7 days";
  if (daysAgo < 30) return "Previous 30 days";
  return "Older";
}

export function SessionSidebar({
  sessions,
  personas,
  activeId,
  generatingIds,
  onSelect,
  onNew,
  onRename,
  onDelete,
  onCollapse,
}: Props) {
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [menuOpen, setMenuOpen] = useState(false);
  const navigate = useNavigate();
  const { data: userSettings } = useUserSettings();

  // A persona either opens a chat or opens a panel; they behave differently enough to
  // deserve separate sections in the menu.
  const deliberationPersonas = personas.filter((p) => p.deliberation);
  const chatPersonas = personas.filter((p) => !p.deliberation);

  // "New chat" either launches the default persona directly (when the user opted into that
  // in Settings → General) or opens the persona picker.
  const defaultPersona = chatPersonas.find((p) => p.is_default);
  const autoDefault =
    !!userSettings?.new_chat_use_default_persona && !!defaultPersona;
  const handleNewChat = () => {
    if (autoDefault) onNew(defaultPersona);
    else setMenuOpen((o) => !o);
  };
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchHit[]>([]);
  const [showArchived, setShowArchived] = useState(false);
  const [showTrash, setShowTrash] = useState(false);
  // Chats and panels share one list; this narrows it without splitting it again.
  const [kind, setKind] = useState<"all" | "chat" | "panel">(
    () => (localStorage.getItem("multichat_sidebar_kind") as "all") || "all",
  );
  const [picked, setPicked] = useState<Set<string>>(new Set());
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
  const queryClient = useQueryClient();
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

  // A running panel settles on its own in the background, so refresh the list while one
  // is in flight — and only then. Nothing else here needs polling.
  const anyRunning = sessions.some(
    (s) => s.status === "running" || s.status === "pending",
  );
  useEffect(() => {
    if (!anyRunning) return;
    const timer = setInterval(
      () => queryClient.invalidateQueries({ queryKey: ["sessions"] }),
      10000,
    );
    return () => clearInterval(timer);
  }, [anyRunning, queryClient]);

  // A run started from the compose screen appears as soon as navigation settles.
  useEffect(() => {
    void queryClient.invalidateQueries({ queryKey: ["sessions"] });
  }, [location.pathname, queryClient]);

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
            onClick={handleNewChat}
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
            {/* Deliberation personas carry a panel + settings, so picking one goes
                straight to the question box with everything already chosen. */}
            {deliberationPersonas.length > 0 && (
              <div className="pb-1">
                <div className="px-3 pb-0.5 text-[10px] font-semibold uppercase tracking-wide text-gray-400">
                  Deliberate
                </div>
                {deliberationPersonas.map((p) => (
                  <button
                    key={p.id}
                    onClick={() => {
                      setMenuOpen(false);
                      navigate(`/d/new?persona=${p.id}`);
                    }}
                    className="block w-full px-3 py-1.5 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-800"
                    title={p.description || ""}
                  >
                    <span className="block truncate">{p.name}</span>
                    <span className="block truncate text-[11px] text-gray-400">
                      {p.description ||
                        `${p.lanes.filter((l) => l.role !== "judge").length} models`}
                    </span>
                  </button>
                ))}
              </div>
            )}
            <button
              onClick={() => {
                setMenuOpen(false);
                navigate("/d/new");
              }}
              title="Put one question to a panel of models and have them review each other"
              className="block w-full px-3 py-1.5 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-800"
            >
              ⚖️ Custom deliberation…
              <span className="block truncate text-[11px] text-gray-400">
                pick the panel yourself
              </span>
            </button>
            <div className="my-1 border-t border-gray-100 dark:border-gray-800" />
            <button
              onClick={() => {
                setMenuOpen(false);
                onNew();
              }}
              className="block w-full px-3 py-1.5 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-800"
            >
              Blank topic
            </button>
            {chatPersonas.length > 0 && (
              <div className="mt-1 border-t border-gray-100 pt-1 dark:border-gray-800">
                <div className="px-3 pb-0.5 text-[10px] font-semibold uppercase tracking-wide text-gray-400">
                  Personas
                </div>
                {chatPersonas.map((p) => (
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
              <Link
                to="/settings"
                className="flex items-center gap-2 rounded px-2 py-1 text-xs text-gray-600 hover:bg-gray-200 dark:text-gray-300 dark:hover:bg-gray-800"
              >
                <span className="w-4 shrink-0 text-center">🔌</span>
                AI Providers
              </Link>
              <Link
                to="/personas"
                className="flex items-center gap-2 rounded px-2 py-1 text-xs text-gray-600 hover:bg-gray-200 dark:text-gray-300 dark:hover:bg-gray-800"
              >
                <span className="w-4 shrink-0 text-center">🎭</span>
                Personas
              </Link>
              <Link
                to="/snippets"
                className="flex items-center gap-2 rounded px-2 py-1 text-xs text-gray-600 hover:bg-gray-200 dark:text-gray-300 dark:hover:bg-gray-800"
              >
                <span className="w-4 shrink-0 text-center">⚡</span>
                Snippets
              </Link>
              <Link
                to="/analytics"
                className="flex items-center gap-2 rounded px-2 py-1 text-xs text-gray-600 hover:bg-gray-200 dark:text-gray-300 dark:hover:bg-gray-800"
              >
                <span className="w-4 shrink-0 text-center">📊</span>
                Usage analytics
              </Link>
              <Link
                to="/evals"
                className="flex items-center gap-2 rounded px-2 py-1 text-xs text-gray-600 hover:bg-gray-200 dark:text-gray-300 dark:hover:bg-gray-800"
              >
                <span className="w-4 shrink-0 text-center">🧪</span>
                Evaluations
              </Link>
              <Link
                to="/integrations"
                className="flex items-center gap-2 rounded px-2 py-1 text-xs text-gray-600 hover:bg-gray-200 dark:text-gray-300 dark:hover:bg-gray-800"
              >
                <span className="w-4 shrink-0 text-center">🔗</span>
                Integrations
              </Link>
              <Link
                to="/settings/general"
                className="flex items-center gap-2 rounded px-2 py-1 text-xs text-gray-600 hover:bg-gray-200 dark:text-gray-300 dark:hover:bg-gray-800"
              >
                <span className="w-4 shrink-0 text-center">🛠</span>
                General
              </Link>
              <Link
                to="/settings/security"
                className="flex items-center gap-2 rounded px-2 py-1 text-xs text-gray-600 hover:bg-gray-200 dark:text-gray-300 dark:hover:bg-gray-800"
              >
                <span className="w-4 shrink-0 text-center">🔒</span>
                Security
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
        {/* Chats and panels live in one list; this narrows it without splitting it. */}
        <div className="mt-1 flex gap-1">
          {(
            [
              ["all", "All"],
              ["chat", "💬 Chats"],
              ["panel", "⚖️ Panels"],
            ] as const
          ).map(([value, label]) => (
            <button
              key={value}
              onClick={() => {
                setKind(value);
                localStorage.setItem("multichat_sidebar_kind", value);
              }}
              className={`flex-1 rounded px-1 py-0.5 text-[10px] ${
                kind === value
                  ? "bg-brand/10 font-semibold text-brand"
                  : "text-gray-500 hover:bg-gray-200 dark:hover:bg-gray-800"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>
      <div className="flex-1 overflow-y-auto">
        {query.trim() ? (
          <div>
            {results.length === 0 ? (
              <div className="p-3 text-xs text-gray-500">No matches.</div>
            ) : (
              results.map((r) => {
                // A hit can belong to a deliberation; send it to the right page.
                const owner = sessions.find((s) => s.id === r.session_id);
                const delib = owner?.mode === "deliberation" && owner.run_id;
                return (
                  <button
                    key={r.session_id}
                    onClick={() => {
                      if (delib) navigate(`/d/${owner!.run_id}`);
                      else onSelect(r.session_id);
                      setQuery("");
                    }}
                    className="block w-full px-3 py-2 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-800"
                  >
                    <div className="truncate font-medium">
                      {delib ? "⚖️ " : "💬 "}
                      {r.title}
                    </div>
                    {r.snippet && (
                      <div className="truncate text-xs text-gray-400">{r.snippet}</div>
                    )}
                  </button>
                );
              })
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
            {renderDateGroups(
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
                {showTrash && (
                  <>
                    {picked.size > 0 && (
                      <div className="flex items-center gap-2 px-3 py-1 text-[10px] text-gray-500">
                        <span>{picked.size} selected</span>
                        <button
                          onClick={() => {
                            if (
                              confirm(
                                `Permanently delete ${picked.size} item(s)? This cannot be undone.`,
                              )
                            ) {
                              picked.forEach((id) => onDelete(id));
                              setPicked(new Set());
                            }
                          }}
                          className="font-medium text-red-500 hover:underline"
                        >
                          Delete selected
                        </button>
                        <button
                          onClick={() => setPicked(new Set())}
                          className="hover:underline"
                        >
                          Clear
                        </button>
                      </div>
                    )}
                    {sessions
                      .filter((s) => s.trashed)
                      .filter(matchesKind)
                      .map((s) => renderRow(s, true))}
                  </>
                )}
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
    const visible = items.filter(matchesKind);
    if (visible.length === 0) return null;
    return (
      <div>
        {label && (
          <div className="px-3 pt-2 text-[10px] font-semibold uppercase tracking-wide text-gray-400">
            {label}
          </div>
        )}
        {visible.map((s) => renderRow(s))}
      </div>
    );
  }

  /**
   * The loose (unfiled) chats, split into Today / Yesterday / … headers by last activity.
   * Dating the SECTION instead of every row is what lets each row be a single line.
   */
  function renderDateGroups(items: SessionListItem[]) {
    const buckets = new Map<string, SessionListItem[]>();
    for (const s of items) {
      const b = dateBucket(s.updated_at);
      const list = buckets.get(b);
      if (list) list.push(s);
      else buckets.set(b, [s]);
    }
    return DATE_BUCKETS.map((label) => (
      <div key={label}>{renderGroup(label, buckets.get(label) ?? [])}</div>
    ));
  }

  /** The All / Chats / Panels filter, applied everywhere rows are listed. */
  function matchesKind(s: SessionListItem) {
    if (kind === "all") return true;
    const isDelib = s.mode === "deliberation";
    return kind === "panel" ? isDelib : !isDelib;
  }

  function renderRow(s: SessionListItem, inTrash = false) {
    // A deliberation is a session like any other; it just opens its own page and reports
    // a verdict instead of a message count.
    const isDelib = s.mode === "deliberation";
    const href = isDelib && s.run_id ? `/d/${s.run_id}` : null;
    const isActive = href ? location.pathname === href : s.id === activeId;
    const open = () => (href ? navigate(href) : onSelect(s.id));
    const verdict =
      s.status === "running" || s.status === "pending"
        ? "running…"
        : s.converged
          ? "converged"
          : (s.status || "").replace("_", " ");
    // Rows are a single line, so everything that used to sit on the second line lives in
    // the hover tooltip instead — nothing is lost, it just costs no vertical space.
    const metaText = [
      s.title,
      isDelib
        ? `${s.lane_count} model${s.lane_count === 1 ? "" : "s"} · ${verdict}${
            s.total_calls ? ` · ${s.total_calls} calls` : ""
          }`
        : `${s.lane_count} lanes · ${s.message_count} msg${
            s.message_count === 1 ? "" : "s"
          }`,
      `${relTime(s.updated_at)} · ${asUtcDate(s.updated_at).toLocaleString()}`,
    ].join("\n");
    return (
      <div
        key={s.id}
        className={`group flex items-center gap-1 px-2 py-1 text-sm ${
          isActive
            ? "bg-brand/10"
            : "hover:bg-gray-100 dark:hover:bg-gray-800"
        }`}
      >
        {inTrash && (
          <input
            type="checkbox"
            checked={picked.has(s.id)}
            onChange={(e) => {
              const next = new Set(picked);
              if (e.target.checked) next.add(s.id);
              else next.delete(s.id);
              setPicked(next);
            }}
            title="Select for bulk delete"
            className="h-3 w-3 shrink-0"
          />
        )}
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
            onClick={open}
            onDoubleClick={() => {
              setEditing(s.id);
              setDraft(s.title);
            }}
            title={metaText}
            className={`flex min-w-0 flex-1 items-center gap-1.5 text-left ${isActive ? "text-brand" : "text-gray-700 dark:text-gray-200"}`}
          >
            {/* Only panels carry a glyph — chats are the norm, so an icon on every row is
                just a column of noise stealing width from the title. */}
            {isDelib && (
              <span className="shrink-0 text-xs" title="Deliberation">
                ⚖️
              </span>
            )}
            <span className="min-w-0 flex-1 truncate font-medium">
              {s.pinned && "📌 "}
              {s.title}
            </span>
            {/* Lane/message counts moved into the row tooltip so each chat is ONE line;
                only a 2–3 character age stays visible, and it yields to the hover actions. */}
            <span className="shrink-0 text-[10px] tabular-nums text-gray-400 group-hover:hidden">
              {shortAge(s.updated_at)}
            </span>
          </button>
        )}
        {generatingIds?.has(s.id) && (
          <svg
            className="h-3.5 w-3.5 shrink-0 animate-spin text-brand group-hover:hidden"
            viewBox="0 0 24 24"
            fill="none"
            aria-label="Generating"
          >
            <title>Generating…</title>
            <circle
              className="opacity-25"
              cx="12"
              cy="12"
              r="10"
              stroke="currentColor"
              strokeWidth="4"
            />
            <path
              className="opacity-75"
              fill="currentColor"
              d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
            />
          </svg>
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
