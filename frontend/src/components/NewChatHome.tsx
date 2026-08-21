import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router";
import type { Attachment, Persona } from "../api/types";
import { useDismiss } from "../hooks/useDismiss";
import { useUserSettings } from "../hooks/useExtras";
import { usePersonas } from "../hooks/usePersonas";
import { useSessions } from "../hooks/useSessions";
import { readLast } from "../utils/lastLocation";
import {
  BLANK_PERSONA,
  readLastPersona,
  rememberLastPersona,
} from "../utils/lastPersona";
import { startersFor } from "../utils/starters";
import { PromptField } from "./ComposerExtras";

/**
 * The landing screen at "/": one prompt box, and the persona that prompt will be answered
 * by, chosen right where it takes effect.
 *
 * The chat is not created until the first prompt is sent — starting a chat used to create
 * an empty session immediately, which littered the sidebar with abandoned "New topic"
 * rows every time someone changed their mind.
 */
export function NewChatHome({
  onStart,
  onSelectSession,
}: {
  /** Create the chat and send the first prompt. `persona` null means a blank topic. */
  onStart: (
    content: string,
    attachmentIds: string[],
    persona: Persona | null,
  ) => Promise<void> | void;
  onSelectSession: (id: string) => void;
}) {
  const navigate = useNavigate();
  const { data: personas = [], isLoading: personasLoading } = usePersonas();
  const { data: sessions = [] } = useSessions();
  const { data: userSettings, isLoading: settingsLoading } = useUserSettings();

  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [personaId, setPersonaId] = useState<string | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const pickerRef = useRef<HTMLDivElement>(null);
  useDismiss(pickerRef, pickerOpen, () => setPickerOpen(false));

  // A persona either opens a chat or opens a panel, and a panel has its own launch
  // screen — so they can't share one list here either.
  const chatPersonas = useMemo(
    () => personas.filter((p) => !p.deliberation),
    [personas],
  );
  const panelPersonas = useMemo(
    () => personas.filter((p) => p.deliberation),
    [personas],
  );

  // Restore the last-used persona once, after the data it is validated against has
  // loaded — resolving early would silently fall back to Blank on every reload.
  const restoredRef = useRef(false);
  useEffect(() => {
    if (restoredRef.current || personasLoading || settingsLoading) return;
    restoredRef.current = true;
    const stored = readLastPersona();
    if (stored === BLANK_PERSONA) return;
    // A remembered persona that has since been deleted falls through to the default.
    if (stored && chatPersonas.some((p) => p.id === stored)) {
      setPersonaId(stored);
      return;
    }
    if (userSettings?.new_chat_use_default_persona) {
      const preferred = chatPersonas.find((p) => p.is_default);
      if (preferred) setPersonaId(preferred.id);
    }
  }, [personasLoading, settingsLoading, chatPersonas, userSettings]);

  const persona = personaId
    ? chatPersonas.find((p) => p.id === personaId) ?? null
    : null;

  function pick(next: Persona | null) {
    setPersonaId(next?.id ?? null);
    rememberLastPersona(next?.id ?? null);
    setPickerOpen(false);
  }

  async function submit() {
    const content = text.trim();
    if (!content || busy) return;
    setBusy(true);
    setError("");
    try {
      rememberLastPersona(persona?.id ?? null);
      await onStart(
        content,
        attachments.map((a) => a.id),
        persona,
      );
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }

  // The remembered conversation is offered rather than forced: landing on "/" used to
  // redirect straight back into it, which made this screen unreachable.
  const resume = useMemo(() => {
    const path = readLast();
    if (!path) return null;
    const match = sessions.find(
      (s) =>
        !s.trashed &&
        (path === `/c/${s.id}` || (!!s.run_id && path === `/d/${s.run_id}`)),
    );
    return match ? { path, title: match.title } : null;
  }, [sessions]);

  const recent = useMemo(
    () =>
      sessions
        .filter((s) => !s.trashed && !s.archived)
        .slice()
        .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
        .slice(0, 5),
    [sessions],
  );

  return (
    <div className="h-full min-w-0 flex-1 overflow-y-auto">
      <div className="flex min-h-full flex-col px-4 py-10">
        <div className="m-auto w-full max-w-2xl">
          <h1 className="mb-6 text-center text-2xl font-semibold text-gray-800 dark:text-gray-100">
            What should we work on?
          </h1>

          <PromptField
            value={text}
            onChange={setText}
            onSubmit={submit}
            attachments={attachments}
            onAttachmentsChange={setAttachments}
            placeholder="Work on anything"
            ariaLabel="Prompt for a new chat"
            autoFocus
            minRows={2}
            maxLines={12}
            disabled={busy}
            footer={
              <>
                <div className="relative" ref={pickerRef}>
                <button
                  type="button"
                  onClick={() => setPickerOpen((o) => !o)}
                  title="Choose the persona that answers this chat"
                  className="flex max-w-[16rem] items-center gap-1 rounded-full border border-gray-300 px-2 py-0.5 text-[11px] text-gray-600 hover:bg-gray-100 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
                >
                  <span>{persona ? "🎭" : "💬"}</span>
                  <span className="truncate">{persona?.name ?? "Blank topic"}</span>
                  <span className="text-gray-400">▾</span>
                </button>
                {pickerOpen && (
                  <div className="absolute top-8 left-0 z-30 max-h-72 w-72 overflow-y-auto rounded-lg border border-gray-200 bg-white py-1 shadow-xl dark:border-gray-700 dark:bg-gray-900">
                    <button
                      type="button"
                      onClick={() => pick(null)}
                      className="block w-full px-3 py-1.5 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-800"
                    >
                      <span className="block truncate">💬 Blank topic</span>
                      <span className="block truncate text-[11px] text-gray-400">
                        no models yet — add one in the chat
                      </span>
                    </button>

                    {chatPersonas.length > 0 && (
                      <div className="mt-1 border-t border-gray-100 pt-1 dark:border-gray-800">
                        <div className="px-3 pb-0.5 text-[10px] font-semibold uppercase tracking-wide text-gray-400">
                          Personas
                        </div>
                        {chatPersonas.map((p) => (
                          <button
                            key={p.id}
                            type="button"
                            onClick={() => pick(p)}
                            title={p.description || ""}
                            className={`block w-full px-3 py-1.5 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-800 ${
                              p.id === personaId ? "text-brand" : ""
                            }`}
                          >
                            <span className="block truncate">
                              {p.is_default ? "⭐ " : ""}
                              {p.name}
                            </span>
                            <span className="block truncate text-[11px] text-gray-400">
                              {p.lanes.length} lane{p.lanes.length === 1 ? "" : "s"}
                              {p.lanes.length > 0 &&
                                ` · ${p.lanes
                                  .map((l) => l.model)
                                  .slice(0, 2)
                                  .join(", ")}`}
                            </span>
                          </button>
                        ))}
                      </div>
                    )}

                    {/* Picking a panel leaves this screen: a deliberation is configured
                        and launched on its own compose page. */}
                    <div className="mt-1 border-t border-gray-100 pt-1 dark:border-gray-800">
                      <div className="px-3 pb-0.5 text-[10px] font-semibold uppercase tracking-wide text-gray-400">
                        Deliberate
                      </div>
                      {panelPersonas.map((p) => (
                        <button
                          key={p.id}
                          type="button"
                          onClick={() => navigate(`/d/new?persona=${p.id}`)}
                          title={p.description || ""}
                          className="block w-full px-3 py-1.5 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-800"
                        >
                          <span className="block truncate">{p.name}</span>
                          <span className="block truncate text-[11px] text-gray-400">
                            {p.description ||
                              `${p.lanes.filter((l) => l.role !== "judge").length} models`}
                          </span>
                        </button>
                      ))}
                      <button
                        type="button"
                        onClick={() => navigate("/d/new")}
                        className="block w-full px-3 py-1.5 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-800"
                      >
                        <span className="block truncate">
                          ⚖️ Custom deliberation…
                        </span>
                        <span className="block truncate text-[11px] text-gray-400">
                          pick the panel yourself
                        </span>
                      </button>
                    </div>

                    <div className="mt-1 border-t border-gray-100 pt-1 dark:border-gray-800">
                      <button
                        type="button"
                        onClick={() => navigate("/personas")}
                        className="block w-full px-3 py-1.5 text-left text-[11px] text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800"
                      >
                        Manage personas →
                      </button>
                    </div>
                  </div>
                )}
                </div>
                <button
                  type="button"
                  onClick={submit}
                  disabled={!text.trim() || busy}
                  title={
                    persona
                      ? `Start a chat with ${persona.name}`
                      : "Create a blank chat and keep this draft — add a model, then send"
                  }
                  className="ml-auto flex h-8 w-8 items-center justify-center rounded-full bg-brand text-white hover:brightness-110 disabled:opacity-40"
                >
                  {busy ? "…" : "↑"}
                </button>
              </>
            }
          />

          {error && <div className="mt-2 text-[11px] text-rose-500">{error}</div>}

          <div className="mt-4 flex flex-wrap justify-center gap-2">
            {startersFor(persona?.system_prompt).map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => setText(s)}
                className="max-w-full truncate rounded-full border border-gray-200 px-3 py-1 text-[11px] text-gray-500 hover:bg-gray-100 dark:border-gray-700 dark:text-gray-400 dark:hover:bg-gray-800"
              >
                {s}
              </button>
            ))}
          </div>

          {resume && (
            <div className="mt-8 text-center">
              <button
                type="button"
                onClick={() => navigate(resume.path)}
                className="text-xs text-brand hover:underline"
              >
                ↩ Continue where you left off — {resume.title}
              </button>
            </div>
          )}

          {recent.length > 0 && (
            <div className="mt-6">
              <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-gray-400">
                Recent
              </div>
              <div className="divide-y divide-gray-100 rounded-lg border border-gray-200 dark:divide-gray-800 dark:border-gray-700">
                {recent.map((s) => (
                  <button
                    key={s.id}
                    type="button"
                    onClick={() =>
                      s.mode === "deliberation" && s.run_id
                        ? navigate(`/d/${s.run_id}`)
                        : onSelectSession(s.id)
                    }
                    className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-gray-50 dark:hover:bg-gray-800"
                  >
                    <span className="shrink-0">
                      {s.mode === "deliberation" ? "⚖️" : "💬"}
                    </span>
                    <span className="min-w-0 flex-1 truncate">{s.title}</span>
                    <span className="shrink-0 text-[11px] text-gray-400">
                      {s.lane_count} lane{s.lane_count === 1 ? "" : "s"}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
