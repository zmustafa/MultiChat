import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import type { Persona } from "../api/types";
import { usePersonas } from "../hooks/usePersonas";
import { useSessions, useSessionMutations } from "../hooks/useSessions";
import { seedLaneCollapse } from "../utils/laneCollapse";
import { SessionSidebar } from "./SessionSidebar";

/**
 * Self-contained navigation sidebar (collapsible) used across all top-level pages
 * so the chat list / menu persists when visiting Settings, Personas, Snippets, etc.
 * Reads the active chat from the URL (/c/:sessionId) or the last-opened chat.
 */
export function SidebarNav() {
  const nav = useNavigate();
  const { sessionId } = useParams<{ sessionId: string }>();
  const activeId = sessionId ?? localStorage.getItem("multichat_active");
  const { data: sessions = [] } = useSessions();
  const { data: personas = [] } = usePersonas();
  const sm = useSessionMutations();
  const [navCollapsed, setNavCollapsed] = useState(
    () => localStorage.getItem("multichat_nav_collapsed") === "1"
  );
  useEffect(() => {
    localStorage.setItem("multichat_nav_collapsed", navCollapsed ? "1" : "0");
  }, [navCollapsed]);

  async function newTopic(persona?: Persona) {
    if (persona) {
      const created = await sm.create.mutateAsync({
        title: persona.name,
        system_prompt: persona.system_prompt || undefined,
        tools_enabled: persona.tools_enabled,
        lanes: persona.lanes.map((l) => ({
          provider_id: l.provider_id,
          model: l.model,
          role: l.role,
        })),
      });
      seedLaneCollapse(
        created.lanes.map((l) => l.id),
        persona.lanes.map((l) => !!l.collapsed)
      );
      nav(`/c/${created.id}`);
      return;
    }
    const created = await sm.create.mutateAsync({ title: "New topic", lanes: [] });
    nav(`/c/${created.id}`);
  }

  if (navCollapsed) {
    return (
      <div className="flex h-full w-11 shrink-0 flex-col items-center gap-1 border-r border-gray-200 bg-gray-50 py-2 dark:border-gray-700 dark:bg-gray-950">
        <button
          onClick={() => setNavCollapsed(false)}
          title="Expand sidebar"
          className="rounded p-1.5 text-gray-500 hover:bg-gray-200 dark:hover:bg-gray-800"
        >
          ⏵
        </button>
        <button
          onClick={() => newTopic(personas.find((p) => p.is_default))}
          title="New chat"
          className="rounded p-1.5 text-gray-500 hover:bg-gray-200 dark:hover:bg-gray-800"
        >
          ✏️
        </button>
        <Link
          to="/settings"
          title="Settings"
          className="rounded p-1.5 text-gray-500 hover:bg-gray-200 dark:hover:bg-gray-800"
        >
          ⚙️
        </Link>
      </div>
    );
  }

  return (
    <SessionSidebar
      sessions={sessions}
      personas={personas}
      activeId={activeId}
      onSelect={(id) => nav(`/c/${id}`)}
      onNew={newTopic}
      onCollapse={() => setNavCollapsed(true)}
      onRename={(id, title) => sm.update.mutate({ id, body: { title } })}
      onDelete={(id) => {
        if (id === activeId) {
          // Auto-focus the next available chat: prefer the one just below the deleted
          // chat, else the one just above, else clear if none remain.
          const idx = sessions.findIndex((s) => s.id === id);
          const pool = sessions.filter((s) => !s.trashed && s.id !== id);
          const next =
            pool.find((s) => sessions.indexOf(s) > idx) ||
            [...pool].reverse().find((s) => sessions.indexOf(s) < idx) ||
            pool[0] ||
            null;
          if (next) {
            localStorage.setItem("multichat_active", next.id);
            nav(`/c/${next.id}`);
          } else {
            localStorage.removeItem("multichat_active");
            nav("/");
          }
        }
        sm.remove.mutate(id);
      }}
    />
  );
}
