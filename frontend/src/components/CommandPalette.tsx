import { useEffect, useMemo, useRef, useState } from "react";
import type { Persona, SessionListItem } from "../api/types";

export interface Command {
  id: string;
  label: string;
  hint?: string;
  run: () => void;
}

interface Props {
  open: boolean;
  onClose: () => void;
  sessions: SessionListItem[];
  personas: Persona[];
  onSelectSession: (id: string) => void;
  onNew: (persona?: Persona) => void;
  onOpenSettings: () => void;
  onToggleTheme: () => void;
  onToggleDiff: () => void;
  extraCommands?: Command[];
}

export function CommandPalette({
  open,
  onClose,
  sessions,
  personas,
  onSelectSession,
  onNew,
  onOpenSettings,
  onToggleTheme,
  onToggleDiff,
  extraCommands = [],
}: Props) {
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setQ("");
      setSel(0);
      setTimeout(() => inputRef.current?.focus(), 10);
    }
  }, [open]);

  const commands: Command[] = useMemo(() => {
    const base: Command[] = [
      { id: "new", label: "New topic", hint: "blank", run: () => { onNew(); onClose(); } },
      ...personas.map((p) => ({
        id: `persona-${p.id}`,
        label: `New topic — ${p.name}`,
        hint: "persona",
        run: () => { onNew(p); onClose(); },
      })),
      { id: "settings", label: "Open Settings", run: () => { onOpenSettings(); onClose(); } },
      { id: "theme", label: "Toggle dark mode", run: () => { onToggleTheme(); onClose(); } },
      { id: "diff", label: "Toggle Diff view", run: () => { onToggleDiff(); onClose(); } },
      ...extraCommands.map((c) => ({ ...c, run: () => { c.run(); onClose(); } })),
      ...sessions.slice(0, 50).map((s) => ({
        id: `sess-${s.id}`,
        label: s.title,
        hint: "go to chat",
        run: () => { onSelectSession(s.id); onClose(); },
      })),
    ];
    const term = q.trim().toLowerCase();
    if (!term) return base;
    return base.filter((c) => c.label.toLowerCase().includes(term));
  }, [q, sessions, personas, extraCommands, onNew, onClose, onSelectSession, onOpenSettings, onToggleTheme, onToggleDiff]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/30 pt-24"
      onClick={onClose}
    >
      <div
        className="w-[32rem] max-w-[90vw] overflow-hidden rounded-lg border border-gray-200 bg-white shadow-2xl dark:border-gray-700 dark:bg-gray-900"
        onClick={(e) => e.stopPropagation()}
      >
        <input
          ref={inputRef}
          value={q}
          onChange={(e) => { setQ(e.target.value); setSel(0); }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") { e.preventDefault(); setSel((s) => Math.min(s + 1, commands.length - 1)); }
            else if (e.key === "ArrowUp") { e.preventDefault(); setSel((s) => Math.max(s - 1, 0)); }
            else if (e.key === "Enter") { e.preventDefault(); commands[sel]?.run(); }
            else if (e.key === "Escape") { onClose(); }
          }}
          placeholder="Type a command or search chats…"
          className="w-full border-b border-gray-200 bg-transparent px-4 py-3 text-sm outline-none dark:border-gray-700"
        />
        <div className="max-h-80 overflow-y-auto py-1">
          {commands.length === 0 && (
            <div className="px-4 py-3 text-sm text-gray-400">No matches.</div>
          )}
          {commands.map((c, i) => (
            <button
              key={c.id}
              onMouseEnter={() => setSel(i)}
              onClick={c.run}
              className={`flex w-full items-center justify-between px-4 py-2 text-left text-sm ${
                i === sel ? "bg-brand/10 text-brand" : "hover:bg-gray-100 dark:hover:bg-gray-800"
              }`}
            >
              <span className="truncate">{c.label}</span>
              {c.hint && <span className="ml-2 shrink-0 text-[10px] text-gray-400">{c.hint}</span>}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
