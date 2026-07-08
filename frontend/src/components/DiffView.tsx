import { useMemo } from "react";
import type { Lane, LaneMessage, Provider, Turn } from "../api/types";

interface Props {
  lanes: Lane[];
  providers: Provider[];
  messages: LaneMessage[];
  turn: Turn | undefined;
}

function normalizeLines(text: string): string[] {
  return text
    .split(/\n+/)
    .map((l) => l.trim())
    .filter(Boolean);
}

export function DiffView({ lanes, providers, messages, turn }: Props) {
  const responders = lanes
    .filter((l) => l.role === "responder")
    .sort((a, b) => a.position - b.position);

  const columns = useMemo(() => {
    return responders.map((lane) => {
      const msg = messages
        .filter(
          (m) => m.lane_id === lane.id && m.turn_id === turn?.id && m.role === "assistant"
        )
        .at(-1);
      return { lane, lines: normalizeLines(msg?.content || "") };
    });
  }, [responders, messages, turn]);

  // lines shared across all lanes = agreement
  const shared = useMemo(() => {
    if (columns.length === 0) return new Set<string>();
    const sets = columns.map((c) => new Set(c.lines.map((l) => l.toLowerCase())));
    const first = sets[0];
    const common = new Set<string>();
    first.forEach((line) => {
      if (sets.every((s) => s.has(line))) common.add(line);
    });
    return common;
  }, [columns]);

  if (!turn) {
    return (
      <div className="p-4 text-sm text-gray-500">No turn selected for diff.</div>
    );
  }

  return (
    <div className="h-full overflow-auto p-3">
      <div className="mb-2 text-xs text-gray-500">
        <span className="mr-3">
          <span className="mr-1 inline-block h-2 w-2 rounded-full bg-green-500" />
          agree
        </span>
        <span>
          <span className="mr-1 inline-block h-2 w-2 rounded-full bg-amber-400" />
          differs
        </span>
      </div>
      <div className="flex gap-3">
        {columns.map(({ lane, lines }) => {
          const provider = providers.find((p) => p.id === lane.provider_id);
          return (
            <div
              key={lane.id}
              className="w-80 shrink-0 rounded border border-gray-300 p-2 text-xs dark:border-gray-700"
            >
              <div className="mb-1 font-semibold">
                {lane.model}
                <span className="ml-1 text-gray-500">{provider?.name}</span>
              </div>
              {lines.map((line, i) => (
                <div
                  key={i}
                  className={`my-0.5 rounded px-1 ${
                    shared.has(line.toLowerCase())
                      ? "bg-green-100 dark:bg-green-900/40"
                      : "bg-amber-50 dark:bg-amber-900/20"
                  }`}
                >
                  {line}
                </div>
              ))}
              {lines.length === 0 && (
                <div className="text-gray-400">(no answer)</div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
