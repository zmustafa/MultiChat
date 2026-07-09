import { useEffect, useState } from "react";
import type { Lane, LaneMessage, Provider, Turn } from "../api/types";
import type { LiveMap } from "../hooks/useBroadcast";
import type { QueuedMessage } from "./LaneComposer";
import { LaneColumn } from "./LaneColumn";

interface Props {
  lanes: Lane[];
  providers: Provider[];
  messages: LaneMessage[];
  turns: Turn[];
  live: LiveMap;
  streaming: boolean;
  bestLaneId: string | null;
  onStop: (laneId: string) => void;
  onRegenerate: (laneId: string) => void;
  onRemove: (laneId: string) => void;
  onPickBest: (laneId: string) => void;
  onContinue?: (laneId: string) => void;
  onBranchTurn?: (turnId: string) => void;
  onPinAnswer?: (laneId: string, turnId: string) => void;
  onEditTurn: (turnId: string, content: string) => void;
  onResendTurn?: (content: string) => void;
  onResendTurnHere?: (content: string, laneId: string) => void;
  onSendToLane?: (content: string, laneId: string) => void;
  onDeleteTurn: (turnId: string) => void;
  onRegenerateTurn: (laneId: string, turnId: string) => void;
  onUpdateLane: (laneId: string, body: { provider_id?: string; model?: string }) => void;
  onCloseLane: (laneId: string) => void;
  onReopenLane: (laneId: string) => void;
  laneWidths: Record<string, number>;
  onResizeLane: (laneId: string, width: number) => void;
  density: "comfortable" | "compact";
  fitToScreen: boolean;
  queuedByLane?: Record<string, QueuedMessage[]>;
  onSendQueuedNow?: (msgId: string, laneId: string) => void;
  onRemoveQueued?: (msgId: string) => void;
}

export function CompareGrid({
  lanes,
  providers,
  messages,
  turns,
  live,
  streaming,
  bestLaneId,
  onStop,
  onRegenerate,
  onRemove,
  onPickBest,
  onContinue,
  onBranchTurn,
  onPinAnswer,
  onEditTurn,
  onResendTurn,
  onResendTurnHere,
  onSendToLane,
  onDeleteTurn,
  onRegenerateTurn,
  onUpdateLane,
  onCloseLane,
  onReopenLane,
  laneWidths,
  onResizeLane,
  density,
  fitToScreen,
  queuedByLane,
  onSendQueuedNow,
  onRemoveQueued,
}: Props) {
  const [maximizedLaneId, setMaximizedLaneId] = useState<string | null>(null);

  const allResponders = lanes
    .filter((l) => l.role === "responder")
    .sort((a, b) => a.position - b.position);
  const responders = allResponders.filter((l) => !l.hidden);
  const closedLanes = allResponders.filter((l) => l.hidden);

  const closedBar =
    closedLanes.length > 0 ? (
      <div className="flex flex-wrap items-center gap-1.5 border-b border-gray-200 bg-gray-50 px-3 py-1.5 dark:border-gray-700 dark:bg-gray-900/40">
        <span className="text-[10px] font-medium uppercase tracking-wide text-gray-400">
          Closed ({closedLanes.length}):
        </span>
        {closedLanes.map((l) => (
          <span
            key={l.id}
            className="inline-flex max-w-[210px] items-center rounded-full border border-gray-300 bg-white text-xs text-gray-600 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300"
          >
            <button
              onClick={() => onReopenLane(l.id)}
              title={`Reopen ${l.model}`}
              className="inline-flex max-w-[170px] items-center gap-1 truncate rounded-l-full py-0.5 pl-2 pr-1 transition hover:text-brand"
            >
              ↩ {bestLaneId === l.id && "⭐ "}
              {l.model}
            </button>
            <button
              onClick={() => {
                if (
                  confirm(
                    `Permanently close the "${l.model}" lane?\n\n` +
                      "This removes the lane and all of its responses in this chat. " +
                      "This cannot be undone."
                  )
                )
                  onRemove(l.id);
              }}
              title={`Permanently close ${l.model}`}
              aria-label={`Permanently close ${l.model}`}
              className="rounded-r-full border-l border-gray-200 py-0.5 pl-1.5 pr-2 text-gray-400 transition hover:text-red-600 dark:border-gray-700 dark:hover:text-red-400"
            >
              ✕
            </button>
          </span>
        ))}
      </div>
    ) : null;

  // If the maximized lane disappears (removed), restore the grid.
  useEffect(() => {
    if (maximizedLaneId && !responders.some((l) => l.id === maximizedLaneId)) {
      setMaximizedLaneId(null);
    }
  }, [maximizedLaneId, responders]);

  // Esc exits maximize.
  useEffect(() => {
    if (!maximizedLaneId) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMaximizedLaneId(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [maximizedLaneId]);

  if (responders.length === 0) {
    return (
      <div className="flex h-full flex-col">
        {closedBar}
        <div className="flex flex-1 items-center justify-center text-sm text-gray-500">
          {closedLanes.length > 0
            ? "All lanes are closed — reopen one above to continue."
            : "Add at least one lane to start comparing."}
        </div>
      </div>
    );
  }

  const toggleMaximize = (laneId: string) =>
    setMaximizedLaneId((cur) => (cur === laneId ? null : laneId));

  const renderLane = (lane: Lane) => {
    const provider = providers.find((p) => p.id === lane.provider_id);
    return (
      <LaneColumn
        key={lane.id}
        lane={lane}
        providerName={provider?.name || "unknown"}
        providers={providers}
        messages={messages}
        turns={turns}
        live={live[lane.id]}
        streaming={streaming}
        isBest={bestLaneId === lane.id}
        onStop={() => onStop(lane.id)}
        onRegenerate={() => onRegenerate(lane.id)}
        onRemove={() => onRemove(lane.id)}
        onPickBest={() => onPickBest(lane.id)}
        onContinue={onContinue ? () => onContinue(lane.id) : undefined}
        onEditTurn={onEditTurn}
        onResendTurn={onResendTurn}
        onResendTurnHere={onResendTurnHere}
        onSendToLane={onSendToLane}
        onDeleteTurn={onDeleteTurn}
        onBranchTurn={onBranchTurn}
        onPinTurn={onPinAnswer ? (turnId) => onPinAnswer(lane.id, turnId) : undefined}
        onRegenerateTurn={(turnId) => onRegenerateTurn(lane.id, turnId)}
        onUpdateLane={(body) => onUpdateLane(lane.id, body)}
        width={laneWidths[lane.id]}
        onResize={(w) => onResizeLane(lane.id, w)}
        isMaximized={maximizedLaneId === lane.id}
        onToggleMaximize={() => toggleMaximize(lane.id)}
        onClose={() => onCloseLane(lane.id)}
        density={density}
        fitToScreen={fitToScreen}
        queued={queuedByLane?.[lane.id]}
        onSendQueuedNow={
          onSendQueuedNow ? (msgId) => onSendQueuedNow(msgId, lane.id) : undefined
        }
        onRemoveQueued={onRemoveQueued}
      />
    );
  };

  if (maximizedLaneId) {
    const maxed = responders.find((l) => l.id === maximizedLaneId);
    const others = responders.filter((l) => l.id !== maximizedLaneId);
    return (
      <div className="flex h-full flex-col p-3">
        {others.length > 0 && (
          <div className="mb-2 flex flex-wrap items-center gap-1.5">
            <button
              onClick={() => setMaximizedLaneId(null)}
              className="rounded border border-gray-300 bg-white px-2 py-0.5 text-xs text-gray-600 hover:bg-gray-100 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-300 dark:hover:bg-gray-800"
              title="Restore all lanes (Esc)"
            >
              🗗 Restore
            </button>
            <span className="text-[10px] uppercase tracking-wide text-gray-400">
              Hidden:
            </span>
            {others.map((l) => (
              <button
                key={l.id}
                onClick={() => setMaximizedLaneId(l.id)}
                title={`Maximize ${l.model}`}
                className="max-w-[160px] truncate rounded-full border border-gray-200 bg-gray-50 px-2 py-0.5 text-xs text-gray-600 hover:border-brand hover:text-brand dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300"
              >
                {bestLaneId === l.id && "⭐ "}
                {l.model}
              </button>
            ))}
          </div>
        )}
        <div className="flex min-h-0 flex-1" data-compare-grid>
          {maxed && renderLane(maxed)}
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      {closedBar}
      <div
        data-compare-grid
        className="flex min-h-0 flex-1 gap-3 overflow-x-auto p-3"
      >
        {responders.map(renderLane)}
      </div>
    </div>
  );
}
