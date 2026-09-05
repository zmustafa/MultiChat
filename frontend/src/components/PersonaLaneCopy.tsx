import { useId, useState } from "react";
import type { Persona, PersonaLane, Provider } from "../api/types";

function laneWarning(lane: PersonaLane, providers: Provider[]): string | null {
  if (!lane.provider_id) {
    return providers.some((provider) => provider.models.includes(lane.model))
      ? null
      : "No configured provider lists this automatic model hint.";
  }
  const provider = providers.find((candidate) => candidate.id === lane.provider_id);
  if (!provider) return "This provider is no longer available.";
  if (!provider.models.includes(lane.model) && provider.default_model !== lane.model) {
    return "This model is not in the provider's current model list; it may need configuring.";
  }
  return null;
}

/** Copies templates verbatim, not launch-time resolved lanes (which can drop/substitute models). */
export function PersonaLaneCopy({
  personas,
  targetId,
  targetName,
  providers,
  providersReady,
  onCopy,
}: {
  personas: Persona[];
  targetId?: string;
  targetName: string;
  providers: Provider[];
  providersReady: boolean;
  onCopy: (lanes: PersonaLane[]) => void;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [sourceId, setSourceId] = useState("");
  const [copiedFrom, setCopiedFrom] = useState<string | null>(null);
  const sources = personas.filter((persona) => persona.id !== targetId);
  const filtered = sources.filter((persona) =>
    `${persona.name} ${persona.description ?? ""}`.toLowerCase().includes(query.trim().toLowerCase()),
  );
  const source = sources.find((persona) => persona.id === sourceId);
  const canCopy = !!source && source.lanes.length > 0 && source.lanes.length <= 6;

  function copy() {
    if (!source || !canCopy) return;
    // PersonaLane contains scalar fields; clone every object as well as the array.
    onCopy(source.lanes.map((lane) => ({ ...lane })));
    setCopiedFrom(source.name);
    setOpen(false);
    setSourceId("");
    setQuery("");
  }

  return (
    <div className="mb-3">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={`${id}-panel`}
        onClick={() => setOpen((value) => !value)}
        className="rounded-lg border border-brand/40 px-2.5 py-1 text-xs font-medium text-brand hover:bg-brand/10"
      >
        Copy lanes from…
      </button>
      {copiedFrom && (
        <p role="status" className="mt-2 text-xs text-green-700 dark:text-green-400">
          Lanes copied from “{copiedFrom}” into this draft. Save persona to keep them.
        </p>
      )}
      {open && (
        <section
          id={`${id}-panel`}
          aria-label="Copy lane configuration"
          className="mt-2 space-y-3 rounded-lg border border-brand/25 bg-brand/5 p-3"
        >
          <p className="text-xs text-gray-600 dark:text-gray-300">
            Copy all lanes from another persona. Your prompt, tools and panel settings stay unchanged.
            This is a one-time copy, not a link between personas.
          </p>
          <div>
            <label htmlFor={`${id}-search`} className="mb-1 block text-xs font-medium text-gray-500">
              Search source personas
            </label>
            <input
              id={`${id}-search`}
              type="search"
              name="persona-lane-source-search"
              autoComplete="off"
              data-lpignore="true"
              data-1p-ignore="true"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Find a persona…"
              className="w-full rounded-lg border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800"
            />
          </div>
          <fieldset className="min-w-0">
            <legend className="mb-1 text-xs font-medium text-gray-500">Source persona</legend>
            <div className="max-h-40 space-y-1 overflow-y-auto">
              {filtered.map((persona) => (
                <label
                  key={persona.id}
                  className={`flex items-center gap-2 rounded-lg border p-2 text-xs ${
                    persona.id === sourceId
                      ? "border-brand bg-brand/10"
                      : "border-transparent hover:bg-gray-100 dark:hover:bg-gray-800"
                  } ${persona.lanes.length === 0 ? "opacity-50" : "cursor-pointer"}`}
                >
                  <input
                    type="radio"
                    name={`${id}-source`}
                    value={persona.id}
                    checked={persona.id === sourceId}
                    disabled={persona.lanes.length === 0}
                    onChange={() => setSourceId(persona.id)}
                  />
                  <span className="min-w-0 flex-1 break-words">{persona.name}</span>
                  {" "}
                  <span className="shrink-0 text-gray-500">
                    {persona.lanes.length === 0 ? "No lanes to copy" : `${persona.lanes.length} lanes`}
                  </span>
                </label>
              ))}
              {filtered.length === 0 && (
                <p className="py-2 text-xs text-gray-500">
                  {sources.length === 0 ? "No other personas available." : "No matching personas."}
                </p>
              )}
            </div>
          </fieldset>
          {source && (
            <div className="space-y-2 border-t border-brand/20 pt-3">
              <h3 className="text-xs font-semibold">Lane preview — {source.name}</h3>
              <ol aria-label="Lanes to copy" className="space-y-1.5">
                {source.lanes.map((lane, index) => {
                  const warning = providersReady ? laneWarning(lane, providers) : null;
                  const provider = providers.find((candidate) => candidate.id === lane.provider_id);
                  return (
                    <li key={index} className="rounded bg-white/70 p-2 text-xs dark:bg-gray-900/70">
                      <p className="break-words font-medium">{index + 1}. {lane.model}</p>
                      <p className="break-words text-gray-500 dark:text-gray-400">
                        {lane.provider_id ? provider?.name ?? "Unavailable provider" : "Automatic provider"}
                        {" · "}{lane.role === "judge" ? "Judge" : "Responder"}
                        {" · "}{lane.collapsed ? "Starts minimized" : "Starts expanded"}
                      </p>
                      {warning && <p className="mt-1 text-amber-700 dark:text-amber-400">{warning}</p>}
                    </li>
                  );
                })}
              </ol>
              {!providersReady && (
                <p className="text-xs text-amber-700 dark:text-amber-400">
                  Provider information is unavailable or still loading; availability cannot be checked yet.
                </p>
              )}
              <p className="text-xs text-gray-500">
                Provider IDs and model names are copied exactly, including automatic hints.
                Unavailable lanes may need fixing before starting a chat.
              </p>
              {source.lanes.length > 6 && (
                <p role="alert" className="text-xs text-amber-700 dark:text-amber-400">
                  A lineup supports up to 6 lanes. Adjust the source before copying; no lanes will be dropped.
                </p>
              )}
              <p className="text-xs font-medium">
                Replace all current lanes in “{targetName || "New persona"}” with the {source.lanes.length} lanes above?
                This replaces any unsaved lane edits. Nothing is saved until you click Save persona.
              </p>
            </div>
          )}
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={() => { setOpen(false); setSourceId(""); setQuery(""); }}
              className="rounded-lg border border-gray-300 px-3 py-1.5 text-xs dark:border-gray-600"
            >
              Cancel copy
            </button>
            <button
              type="button"
              onClick={copy}
              disabled={!canCopy}
              className="rounded-lg bg-brand px-3 py-1.5 text-xs font-medium text-white hover:brightness-110 disabled:opacity-40"
            >
              Replace lanes
            </button>
          </div>
        </section>
      )}
    </div>
  );
}