import { useEffect, useMemo, useRef, useState } from "react";
import type { Lane, LaneMessage, Provider, Turn } from "../api/types";
import { mediaUrl } from "../api/client";
import type { LiveLane } from "../hooks/useBroadcast";
import { addArtifact } from "../hooks/useArtifacts";
import { isLaneCollapsed, setLaneCollapsedState } from "../utils/laneCollapse";
import { contentBadges } from "../utils/contentMeta";
import type { QueuedMessage } from "./LaneComposer";
import { MessageRenderer, CodeFoldContext } from "./MessageRenderer";
import { ToolCallCard } from "./ToolCallCard";

interface Props {
  lane: Lane;
  providerName: string;
  providers?: Provider[];
  messages: LaneMessage[];
  turns: Turn[];
  live?: LiveLane;
  streaming: boolean;
  isBest: boolean;
  onStop: () => void;
  onRegenerate: () => void;
  onRemove: () => void;
  onPickBest: () => void;
  onContinue?: () => void;
  onEditTurn?: (turnId: string, content: string) => void;
  onResendTurn?: (content: string) => void;
  onResendTurnHere?: (content: string, laneId: string) => void;
  onSendToLane?: (content: string, laneId: string) => void;
  onDeleteTurn?: (turnId: string) => void;
  onBranchTurn?: (turnId: string) => void;
  onPinTurn?: (turnId: string) => void;
  onRegenerateTurn?: (turnId: string) => void;
  onUpdateLane?: (body: { provider_id?: string; model?: string }) => void;
  width?: number;
  onResize?: (width: number) => void;
  isMaximized?: boolean;
  onToggleMaximize?: () => void;
  onClose?: () => void;
  density?: "comfortable" | "compact";
  fitToScreen?: boolean;
  queued?: QueuedMessage[];
  onSendQueuedNow?: (msgId: string) => void;
  onRemoveQueued?: (msgId: string) => void;
}

function CopyIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="9" y="9" width="13" height="13" rx="2" />
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
    </svg>
  );
}

function RegenIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M23 4v6h-6" />
      <path d="M1 20v-6h6" />
      <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M20 6 9 17l-5-5" />
    </svg>
  );
}

/** Small icon action button shown at the bottom of a response. */
function ResponseActions({
  content,
  onRegenerate,
  regenerateDisabled,
  onPin,
}: {
  content: string;
  onRegenerate?: () => void;
  regenerateDisabled?: boolean;
  onPin?: () => void;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex items-center gap-1 text-gray-400">
      <button
        onClick={() => {
          navigator.clipboard.writeText(content);
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        }}
        title="Copy response"
        className="rounded p-1 transition hover:bg-gray-100 hover:text-gray-700 dark:hover:bg-gray-800 dark:hover:text-gray-200"
      >
        {copied ? <CheckIcon /> : <CopyIcon />}
      </button>
      {onPin && (
        <button
          onClick={onPin}
          title="Pin this answer to compare across runs"
          className="rounded p-1 transition hover:bg-gray-100 hover:text-gray-700 dark:hover:bg-gray-800 dark:hover:text-gray-200"
        >
          📌
        </button>
      )}
      {onRegenerate && (
        <button
          onClick={onRegenerate}
          disabled={regenerateDisabled}
          title="Regenerate response"
          className="rounded p-1 transition hover:bg-gray-100 hover:text-gray-700 disabled:cursor-not-allowed disabled:opacity-40 dark:hover:bg-gray-800 dark:hover:text-gray-200"
        >
          <RegenIcon />
        </button>
      )}
    </div>
  );
}

function CopyBtn({ text, label = "Copy" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      onClick={() => {
        navigator.clipboard.writeText(text);
        setDone(true);
        setTimeout(() => setDone(false), 1200);
      }}
      title="Copy to clipboard"
      className="rounded px-1 text-[10px] text-gray-400 opacity-0 transition hover:text-gray-700 group-hover:opacity-100 dark:hover:text-gray-200"
    >
      {done ? "✓ Copied" : label}
    </button>
  );
}

const SOURCES_LIMIT = 3;

function SourcesStrip({ messages }: { messages: LaneMessage[] }) {
  const [expanded, setExpanded] = useState(false);
  const seen = new Set<string>();
  const sources: { title: string; url: string }[] = [];
  for (const m of messages) {
    for (const tc of m.tool_calls) {
      for (const c of (tc.citations_json || []) as any[]) {
        if (c?.url && !seen.has(c.url)) {
          seen.add(c.url);
          sources.push({ title: c.title || c.url, url: c.url });
        }
      }
    }
  }
  if (sources.length === 0) return null;
  const hasMore = sources.length > SOURCES_LIMIT;
  const visible = expanded ? sources : sources.slice(0, SOURCES_LIMIT);
  return (
    <div className="mt-1.5 rounded border border-gray-100 bg-gray-50 p-1.5 text-[10px] dark:border-gray-800 dark:bg-gray-800/50">
      <div className="mb-0.5 font-semibold uppercase tracking-wide text-gray-400">
        Sources
      </div>
      <ol className="list-decimal space-y-0.5 pl-4">
        {visible.map((s, i) => (
          <li key={i}>
            <a
              href={s.url}
              target="_blank"
              rel="noreferrer"
              className="text-blue-500 hover:underline"
              title={s.url}
            >
              {s.title}
            </a>
          </li>
        ))}
      </ol>
      {hasMore && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="mt-1 font-medium text-blue-500 hover:underline"
        >
          {expanded
            ? "Show less"
            : `Show ${sources.length - SOURCES_LIMIT} more`}
        </button>
      )}
    </div>
  );
}

const STATUS_DOT: Record<string, string> = {
  idle: "bg-gray-400",
  queued: "bg-amber-400",
  streaming: "bg-blue-500 animate-pulse",
  done: "bg-green-500",
  error: "bg-red-500",
};

function StatusBadge({ state }: { state: string }) {
  return (
    <span className="inline-flex items-center gap-1 text-xs">
      {state === "streaming" ? (
        <svg
          className="h-3 w-3 shrink-0 animate-spin text-blue-500"
          viewBox="0 0 24 24"
          fill="none"
          aria-hidden="true"
        >
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
      ) : (
        <span className={`h-2 w-2 rounded-full ${STATUS_DOT[state] || "bg-gray-400"}`} />
      )}
      {state}
    </span>
  );
}

/** Inline editor in the lane header to change the lane's provider/model. */
function LaneModelEditor({
  lane,
  providers,
  onSave,
  onCancel,
}: {
  lane: Lane;
  providers: Provider[];
  onSave: (body: { provider_id?: string; model?: string }) => void;
  onCancel: () => void;
}) {
  const [providerId, setProviderId] = useState(lane.provider_id);
  const [model, setModel] = useState(lane.model);
  const provider = providers.find((p) => p.id === providerId);
  const models = provider?.models || [];

  return (
    <div className="flex flex-1 flex-col gap-1">
      <select
        value={providerId}
        onChange={(e) => {
          setProviderId(e.target.value);
          const p = providers.find((pp) => pp.id === e.target.value);
          setModel(p?.default_model || p?.models[0] || "");
        }}
        className="w-full rounded border border-gray-300 px-1 py-0.5 text-xs dark:border-gray-600 dark:bg-gray-800"
      >
        {providers.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name} ({p.provider_type})
          </option>
        ))}
      </select>
      <div className="flex items-center gap-1">
        {models.length > 0 ? (
          <select
            value={models.includes(model) ? model : ""}
            onChange={(e) => setModel(e.target.value)}
            className="min-w-0 flex-1 rounded border border-gray-300 px-1 py-0.5 text-xs dark:border-gray-600 dark:bg-gray-800"
          >
            <option value="">Select model…</option>
            {models.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        ) : (
          <input
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder="model id"
            className="min-w-0 flex-1 rounded border border-gray-300 px-1 py-0.5 text-xs dark:border-gray-600 dark:bg-gray-800"
          />
        )}
        <button
          onClick={() => model.trim() && onSave({ provider_id: providerId, model: model.trim() })}
          disabled={!model.trim()}
          title="Save model"
          className="rounded px-1 text-xs text-green-600 hover:bg-green-50 disabled:opacity-40 dark:hover:bg-green-950"
        >
          ✓
        </button>
        <button
          onClick={onCancel}
          title="Cancel"
          className="rounded px-1 text-xs text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800"
        >
          ✕
        </button>
      </div>
    </div>
  );
}

/**
 * The blue "You" prompt that freezes (sticky) at the top of a lane's transcript while you
 * scroll through its answer. The prompt text is clamped to 2 lines ONLY while the header is
 * actually pinned to the top; in normal scroll position it shows the full prompt. A
 * zero-height sentinel just above the sticky header lets an IntersectionObserver detect the
 * pinned state: once the sentinel scrolls above the scroll viewport's top edge, the header
 * is stuck.
 */
function StickyYouHeader({
  turn,
  scrollRef,
  onResendTurn,
  onResendHere,
  onEditTurn,
  onDeleteTurn,
  onBranchTurn,
}: {
  turn: Turn;
  scrollRef: React.RefObject<HTMLDivElement>;
  onResendTurn?: (content: string) => void;
  onResendHere?: (content: string) => void;
  onEditTurn?: (turnId: string, content: string) => void;
  onDeleteTurn?: (turnId: string) => void;
  onBranchTurn?: (turnId: string) => void;
}) {
  const sentinelRef = useRef<HTMLDivElement>(null);
  const [stuck, setStuck] = useState(false);
  useEffect(() => {
    const root = scrollRef.current;
    const sentinel = sentinelRef.current;
    if (!root || !sentinel) return;
    const io = new IntersectionObserver(
      ([entry]) => {
        const rootTop =
          entry.rootBounds?.top ?? root.getBoundingClientRect().top;
        setStuck(entry.boundingClientRect.top < rootTop + 1);
      },
      { root, threshold: [0, 1] },
    );
    io.observe(sentinel);
    return () => io.disconnect();
  }, [scrollRef]);

  return (
    <>
      <div ref={sentinelRef} className="h-0" aria-hidden />
      <div className="group sticky top-0 z-20 rounded border border-blue-200 bg-gradient-to-br from-blue-100 to-blue-300 px-2 py-1 shadow-sm backdrop-blur-sm dark:border-blue-900/40 dark:from-blue-950 dark:to-blue-900">
        <div className="flex items-center justify-between">
          <span className="text-[10px] font-semibold uppercase tracking-wide text-blue-700 dark:text-blue-300">
            You
          </span>
          <span className="flex items-center gap-1">
            {onResendTurn && turn.content && (
              <button
                onClick={() => onResendTurn(turn.content)}
                title="Send this prompt again to ALL lanes"
                className="rounded px-1 text-[10px] text-gray-400 opacity-0 transition hover:text-brand group-hover:opacity-100"
              >
                Send all
              </button>
            )}
            {onResendHere && turn.content && (
              <button
                onClick={() => onResendHere(turn.content)}
                title="Send this prompt again to THIS lane only"
                className="rounded px-1 text-[10px] text-gray-400 opacity-0 transition hover:text-brand group-hover:opacity-100"
              >
                Send here
              </button>
            )}
            {onEditTurn && turn.content && (
              <button
                onClick={() => onEditTurn(turn.id, turn.content)}
                title="Edit & resend"
                className="rounded px-1 text-[10px] text-gray-400 opacity-0 transition hover:text-gray-700 group-hover:opacity-100 dark:hover:text-gray-200"
              >
                Edit
              </button>
            )}
            {onDeleteTurn && (
              <button
                onClick={() => onDeleteTurn(turn.id)}
                title="Delete this turn"
                className="rounded px-1 text-[10px] text-gray-400 opacity-0 transition hover:text-red-500 group-hover:opacity-100"
              >
                Delete
              </button>
            )}
            {onBranchTurn && (
              <button
                onClick={() => onBranchTurn(turn.id)}
                title="Branch a new session from this turn"
                className="rounded px-1 text-[10px] text-gray-400 opacity-0 transition hover:text-brand group-hover:opacity-100"
              >
                ⌥ Branch
              </button>
            )}
            {turn.content && <CopyBtn text={turn.content} />}
          </span>
        </div>
        {turn.attachments.length > 0 && (
          <div className="mt-1 flex flex-wrap gap-1.5">
            {turn.attachments.map((a) =>
              a.kind === "document" ? (
                <a
                  key={a.id}
                  href={mediaUrl(a.url)}
                  target="_blank"
                  rel="noreferrer"
                  title={a.filename}
                  className="flex items-center gap-1 rounded border border-gray-200 bg-gray-50 px-2 py-1 text-xs text-gray-600 hover:border-brand dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300"
                >
                  <span>📄</span>
                  <span className="max-w-[140px] truncate">{a.filename}</span>
                </a>
              ) : (
                <a
                  key={a.id}
                  href={mediaUrl(a.url)}
                  target="_blank"
                  rel="noreferrer"
                  title={a.filename}
                >
                  <img
                    src={mediaUrl(a.url)}
                    alt={a.filename}
                    className="h-16 w-16 rounded border border-gray-200 object-cover dark:border-gray-700"
                  />
                </a>
              ),
            )}
          </div>
        )}
        {turn.content && (
          <div
            className={`mt-1 whitespace-pre-wrap break-words text-sm text-gray-800 dark:text-gray-100 ${
              stuck ? "line-clamp-2" : ""
            }`}
            title={stuck ? turn.content : undefined}
          >
            {turn.content}
          </div>
        )}
      </div>
    </>
  );
}

export function LaneColumn({
  lane,
  providerName,
  providers,
  messages,
  turns,
  live,
  streaming,
  isBest,
  onStop,
  onRegenerate,
  onRemove,
  onPickBest,
  onContinue,
  onEditTurn,
  onResendTurn,
  onResendTurnHere,
  onSendToLane,
  onDeleteTurn,
  onBranchTurn,
  onPinTurn,
  onRegenerateTurn,
  onUpdateLane,
  width,
  onResize,
  isMaximized,
  onToggleMaximize,
  onClose,
  density = "comfortable",
  fitToScreen,
  queued,
  onSendQueuedNow,
  onRemoveQueued,
}: Props) {
  const [collapsed, setCollapsed] = useState(() => isLaneCollapsed(lane.id));
  const [editingModel, setEditingModel] = useState(false);
  const [laneDraft, setLaneDraft] = useState("");
  // Drives the per-lane "collapse/expand all code" control; consumed by every CodeBlock.
  const [codeFold, setCodeFold] = useState({ signal: 0, collapsed: false });
  const rootRef = useRef<HTMLDivElement>(null);

  const sendLaneDraft = () => {
    const content = laneDraft.trim();
    if (!content) return;
    onSendToLane?.(content, lane.id);
    setLaneDraft("");
  };

  const toggleCollapsed = (next?: boolean) =>
    setCollapsed((c) => {
      const v = next ?? !c;
      setLaneCollapsedState(lane.id, v);
      return v;
    });

  function startResize(e: React.MouseEvent) {
    if (!onResize) return;
    e.preventDefault();
    const startX = e.clientX;
    const startW = rootRef.current?.offsetWidth ?? width ?? 320;
    const onMove = (ev: MouseEvent) => {
      const w = Math.max(240, Math.min(1000, startW + (ev.clientX - startX)));
      onResize(w);
    };
    const onUp = () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }
  const status = live?.status || lane.state;
  // Memoize the per-render derived data (message filtering, the turn->message map, and the
  // sorted/filtered turn list). Without this every lane recomputes all of it on every
  // frame while ANY lane streams; memoizing keeps idle lanes cheap.
  const laneMessages = useMemo(
    () => messages.filter((m) => m.lane_id === lane.id && m.role === "assistant"),
    [messages, lane.id],
  );
  const byTurn = useMemo(
    () => new Map(laneMessages.map((m) => [m.turn_id, m])),
    [laneMessages],
  );
  const orderedTurns = useMemo(
    () =>
      [...turns]
        .sort((a, b) => a.order_index - b.order_index)
        .filter(
          (t) => !t.target_lane_ids_json || t.target_lane_ids_json.includes(lane.id),
        ),
    [turns, lane.id],
  );

  // When a (re)generation starts we snapshot the turn's currently-persisted answer. This
  // lets us tell, once the lane finishes, whether the refetched message already reflects
  // the NEW answer — so we can keep showing the freshly streamed text until it does,
  // instead of briefly flashing the STALE old answer during the ~250ms refetch window.
  const regenBaselineRef = useRef<Map<string, string>>(new Map());
  useEffect(() => {
    const t = live?.turnId;
    if (!t) return;
    if (
      (live.status === "streaming" || live.status === "queued") &&
      (live.content ?? "") === ""
    ) {
      regenBaselineRef.current.set(t, byTurn.get(t)?.content ?? "");
    }
  }, [live?.turnId, live?.status, byTurn]);

  // Whether this lane has any fenced code blocks — gates the "collapse/expand all code" btn.
  const hasCode = useMemo(
    () =>
      laneMessages.some((m) => m.content.includes("```")) ||
      !!live?.content?.includes("```"),
    [laneMessages, live?.content],
  );

  // Auto-scroll the transcript to the bottom as content streams in — but only while
  // the user is "stuck" to the bottom. If they scroll up to read earlier content, we
  // pause following the stream; scrolling back near the bottom re-enables it, and a new
  // message (new turn) always resets it so the next response follows again.
  const scrollRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const prevScrollTop = useRef(0);
  const programmaticScroll = useRef(false);
  const handleScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const cur = el.scrollTop;
    const nearBottom = el.scrollHeight - cur - el.clientHeight < 60;
    // Our own scroll-to-bottom fires a scroll event too — never treat it as the user
    // scrolling away. Streaming content also grows scrollHeight, which can momentarily
    // put the position "off bottom"; that must NOT pause following. We only pause when
    // the position actually moves UP (user wheel/drag), which lowers scrollTop.
    if (programmaticScroll.current) {
      programmaticScroll.current = false;
    } else if (nearBottom) {
      stickToBottom.current = true;
    } else if (cur < prevScrollTop.current - 2) {
      stickToBottom.current = false;
    }
    prevScrollTop.current = cur;
  };
  // Reset stickiness only when a NEW message starts (the live turn id changes), so the
  // next response follows to the bottom even if the user scrolled up during the previous
  // one. We deliberately do NOT reset when the current message merely finishes — that
  // would yank a user who scrolled up back to the bottom the moment streaming ends.
  useEffect(() => {
    stickToBottom.current = true;
  }, [live?.turnId]);
  useEffect(() => {
    const el = scrollRef.current;
    if (el && stickToBottom.current) {
      programmaticScroll.current = true;
      el.scrollTop = el.scrollHeight;
      prevScrollTop.current = el.scrollTop;
    }
  }, [
    live?.content,
    live?.status,
    live?.toolCalls.length,
    laneMessages.length,
    orderedTurns.length,
  ]);

  // Follow late, ASYNCHRONOUS height changes too. Some content grows the transcript AFTER
  // streaming finished — most notably Mermaid diagrams (which render ~300ms after the code
  // settles) and images that load in. Those don't change any of the effect deps above, so
  // without this the lane would be left short of the bottom. A ResizeObserver on the
  // content re-pins to the bottom whenever it grows while the user is stuck there.
  useEffect(() => {
    const el = scrollRef.current;
    const content = contentRef.current;
    if (!el || !content || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => {
      if (stickToBottom.current) {
        programmaticScroll.current = true;
        el.scrollTop = el.scrollHeight;
        prevScrollTop.current = el.scrollTop;
      }
    });
    ro.observe(content);
    return () => ro.disconnect();
  }, []);

  return (
    <div
      ref={rootRef}
      style={
        !collapsed && !isMaximized && width
          ? { width, flexGrow: 0, flexShrink: 0 }
          : undefined
      }
      className={`relative flex flex-col rounded-lg border border-gray-300 bg-white dark:border-gray-700 dark:bg-gray-900 ${
        collapsed
          ? "w-10 shrink-0"
          : isMaximized
            ? "min-w-0 flex-1 basis-0"
            : width
              ? "shrink-0"
              : fitToScreen
                ? "min-w-0 flex-1 basis-0"
                : "min-w-[280px] flex-1 basis-0"
      }`}
    >
      {!collapsed && !isMaximized && onResize && (
        <div
          onMouseDown={startResize}
          onDoubleClick={() => onResize(0)}
          title="Drag to resize · double-click to auto-fit"
          className="absolute right-0 top-0 z-10 h-full w-1.5 cursor-col-resize rounded-r-lg hover:bg-brand/40"
        />
      )}
      {collapsed ? (
        <div className="flex h-full flex-col items-center gap-2 py-2">
          <button
            onClick={() => toggleCollapsed(false)}
            title="Expand"
            className="text-xs text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
          >
            ▸
          </button>
          <span
            className={`h-2 w-2 shrink-0 rounded-full ${STATUS_DOT[status] || "bg-gray-400"}`}
            title={status}
          />
          <button
            onClick={() => toggleCollapsed(false)}
            title={lane.model}
            className="min-h-0 flex-1 overflow-hidden text-xs font-semibold text-gray-600 hover:text-brand dark:text-gray-300"
            style={{ writingMode: "vertical-rl", textOrientation: "mixed" }}
          >
            {isBest && "⭐ "}
            {lane.model}
          </button>
          <button
            onClick={onClose ?? onRemove}
            title="Close lane"
            className="text-xs text-gray-400 hover:text-red-500"
          >
            ✕
          </button>
        </div>
      ) : (
      <div className="flex items-center justify-between gap-1 border-b border-gray-200 px-2 py-1.5 dark:border-gray-700">
        {editingModel && providers && onUpdateLane ? (
          <LaneModelEditor
            lane={lane}
            providers={providers}
            onSave={(body) => {
              onUpdateLane(body);
              setEditingModel(false);
            }}
            onCancel={() => setEditingModel(false)}
          />
        ) : (
          <>
            <div className="min-w-0">
              <div className="flex items-center gap-1">
                <span className="truncate text-sm font-semibold" title={lane.model}>
                  {isBest && "⭐ "}
                  {lane.model}
                  {lane.role === "judge" && " (judge)"}
                </span>
                {providers && onUpdateLane && (
                  <button
                    onClick={() => setEditingModel(true)}
                    title="Change model"
                    className="shrink-0 text-xs text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
                  >
                    ✎
                  </button>
                )}
              </div>
              <div className="truncate text-xs text-gray-500 dark:text-gray-400">{providerName}</div>
            </div>
            <div className="flex items-center gap-0.5">
              {hasCode && (
                <button
                  onClick={() =>
                    setCodeFold((f) => ({
                      signal: f.signal + 1,
                      collapsed: !f.collapsed,
                    }))
                  }
                  title={
                    codeFold.collapsed
                      ? "Expand all code blocks"
                      : "Collapse all code blocks"
                  }
                  className="rounded px-1 text-xs text-gray-400 hover:bg-gray-100 hover:text-gray-700 dark:hover:bg-gray-800 dark:hover:text-gray-200"
                >
                  {codeFold.collapsed ? "▸</>" : "▾</>"}
                </button>
              )}
              <StatusBadge state={status} />
              {onToggleMaximize && !collapsed && (
                <button
                  onClick={onToggleMaximize}
                  className="rounded px-1 text-xs text-gray-400 hover:bg-gray-100 hover:text-gray-700 dark:hover:bg-gray-800 dark:hover:text-gray-200"
                  title={isMaximized ? "Restore" : "Maximize"}
                >
                  {isMaximized ? "🗗" : "🗖"}
                </button>
              )}
              <button
                onClick={() => toggleCollapsed()}
                className="rounded px-1 text-xs text-gray-400 hover:bg-gray-100 hover:text-gray-700 dark:hover:bg-gray-800 dark:hover:text-gray-200"
                title={collapsed ? "Expand" : "Collapse"}
              >
                {collapsed ? "▸" : "▾"}
              </button>
              <button
                onClick={onClose ?? onRemove}
                className="rounded px-1 text-xs text-gray-400 hover:bg-red-50 hover:text-red-500 dark:hover:bg-red-950"
                title="Close lane (restore later)"
              >
                ✕
              </button>
            </div>
          </>
        )}
      </div>
      )}

      {!collapsed && (
        <>
          <div
            ref={scrollRef}
            onScroll={handleScroll}
            className={`min-h-0 flex-1 overflow-y-auto ${
              density === "compact" ? "p-1.5 text-[13px]" : "p-2"
            }`}
          >
            <CodeFoldContext.Provider value={codeFold}>
            <div
              ref={contentRef}
              className={density === "compact" ? "space-y-1.5" : "space-y-3"}
            >
            {orderedTurns.map((turn, turnIdx) => {
              const m = byTurn.get(turn.id);
              // Skip rendering/layout for off-screen turns of long transcripts. The browser
              // reuses `contain-intrinsic-size` as a placeholder so scroll height stays
              // stable. We exclude the last turn (in view / streaming) so the bottom stays
              // pixel-accurate for auto-scroll.
              const virtualize = turnIdx < orderedTurns.length - 1;
              // Every turn's prompt is frozen (Excel-style): it sticks to the top of the
              // transcript while you scroll through its (often long) answer, and scrolls
              // away once you scroll past it — the next turn's prompt then takes over.
              // Each sticky header is bounded by its own turn container, so they hand off.
              // While this turn is (re)generating, show the live stream in place of the
              // stale persisted answer — but once the lane is DONE and its message is
              // persisted, always show the persisted message (which carries the footer:
              // copy/pin/regenerate + latency/tokens). We must NOT require an exact
              // content match here: the saved answer often differs slightly from the
              // streamed text (appended file links, a final tools-disabled completion,
              // trailing whitespace), which used to leave the live overlay covering the
              // real answer — no footer — until a full page refresh cleared it.
              //
              // On DONE, however, keep showing the freshly streamed text until the
              // persisted message actually reflects the NEW answer. Otherwise the lane
              // briefly flashes the OLD answer (and remounts the renderer) during the
              // ~250ms session-refetch window after a single-lane regenerate. "Reflects
              // the new answer" = the persisted content equals the streamed text, or it
              // has changed from the snapshot taken when this (re)generation started.
              const liveContent = live?.content ?? "";
              const persistedReflectsNew =
                !!m &&
                (m.content === liveContent ||
                  m.content !== (regenBaselineRef.current.get(turn.id) ?? m.content));
              const liveHere =
                live &&
                live.turnId === turn.id &&
                live.status !== "error" &&
                !(
                  m &&
                  (live.status === "done"
                    ? persistedReflectsNew
                    : m.content === live.content)
                )
                  ? live
                  : null;
              return (
                <div
                  key={turn.id}
                  className="space-y-1.5 border-b border-gray-100 pb-2 dark:border-gray-800"
                  style={
                    virtualize
                      ? ({
                          contentVisibility: "auto",
                          containIntrinsicSize: "auto 300px",
                        } as React.CSSProperties)
                      : undefined
                  }
                >
                  {(turn.content || turn.attachments.length > 0) && (
                    <StickyYouHeader
                      turn={turn}
                      scrollRef={scrollRef}
                      onResendTurn={onResendTurn}
                      onResendHere={
                        onResendTurnHere
                          ? (content) => onResendTurnHere(content, lane.id)
                          : undefined
                      }
                      onEditTurn={onEditTurn}
                      onDeleteTurn={onDeleteTurn}
                      onBranchTurn={onBranchTurn}
                    />
                  )}
                  {liveHere && (
                    <div>
                      <div className="text-[10px] font-semibold uppercase tracking-wide text-gray-400">
                        {lane.model}
                      </div>
                      {(liveHere.statusText ||
                        liveHere.status === "streaming" ||
                        liveHere.status === "queued") && (
                        <div className="mb-1 flex items-center gap-1.5 text-xs text-blue-500">
                          <svg
                            className="h-3.5 w-3.5 shrink-0 animate-spin text-blue-500"
                            viewBox="0 0 24 24"
                            fill="none"
                            aria-hidden="true"
                          >
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
                          {liveHere.statusText ||
                            (liveHere.status === "queued"
                              ? "Queued…"
                              : "Request sent · awaiting response…")}
                        </div>
                      )}
                      {liveHere.toolCalls.map((tc) => (
                        <ToolCallCard key={tc.tool_call_id} call={tc} />
                      ))}
                      <MessageRenderer content={liveHere.content || "…"} />
                    </div>
                  )}
                  {!liveHere && m && (
                    <div className="group">
                      <div className="flex items-center justify-between">
                        <span className="flex min-w-0 items-center gap-1">
                          <span className="truncate text-[10px] font-semibold uppercase tracking-wide text-gray-400">
                            {lane.model}
                          </span>
                          {!m.error &&
                            m.content &&
                            contentBadges(m.content).map((b) => (
                              <span
                                key={b.label}
                                title={`contains a ${b.label}`}
                                className="shrink-0 rounded-full bg-gray-100 px-1.5 text-[9px] font-medium text-gray-500 dark:bg-gray-800 dark:text-gray-400"
                              >
                                {b.icon} {b.label}
                              </span>
                            ))}
                        </span>
                        {!m.error && m.content && (
                          <span className="flex items-center gap-1">
                            <button
                              onClick={() =>
                                addArtifact(m.content, `${lane.model}: ${turn.content}`)
                              }
                              title="Pin to Artifacts"
                              className="rounded px-1 text-[10px] text-gray-400 opacity-0 transition hover:text-gray-700 group-hover:opacity-100 dark:hover:text-gray-200"
                            >
                              📌
                            </button>
                            <CopyBtn text={m.content} />
                          </span>
                        )}
                      </div>
                      {m.tool_calls.map((tc) => (
                        <ToolCallCard
                          key={tc.id}
                          call={{
                            tool_call_id: tc.id,
                            tool: tc.tool_name,
                            arguments: tc.arguments_json,
                            status: tc.status,
                            result: tc.result_json?.result,
                            citations: tc.citations_json,
                          }}
                        />
                      ))}
                      {m.error ? (
                        <div className="text-xs text-red-500">Error: {m.error}</div>
                      ) : (
                        <MessageRenderer content={m.content} />
                      )}
                      <SourcesStrip messages={[m]} />
                      {!m.error && m.content && (
                        <div className="mt-1.5 flex items-center gap-2">
                          <ResponseActions
                            content={m.content}
                            onRegenerate={
                              onRegenerateTurn
                                ? () => onRegenerateTurn(turn.id)
                                : undefined
                            }
                            onPin={onPinTurn ? () => onPinTurn(turn.id) : undefined}
                            regenerateDisabled={
                              status === "streaming" || status === "queued"
                            }
                          />
                          {(m.latency_ms != null || m.usage_json) && (
                            <span className="text-[10px] text-gray-500 dark:text-gray-400">
                              {m.latency_ms != null && `${m.latency_ms} ms`}
                              {m.usage_json?.completion_tokens != null &&
                                ` · ${m.usage_json.completion_tokens} tok`}
                              {m.usage_json?.completion_tokens != null &&
                                m.latency_ms != null &&
                                m.latency_ms > 0 &&
                                ` · ${(
                                  m.usage_json.completion_tokens /
                                  (m.latency_ms / 1000)
                                ).toFixed(1)} tok/s`}
                            </span>
                          )}
                        </div>
                      )}
                      {m.error && (m.latency_ms != null || m.usage_json) && (
                        <div className="mt-1 text-[10px] text-gray-500 dark:text-gray-400">
                          {m.latency_ms != null && `${m.latency_ms} ms`}
                          {m.usage_json?.completion_tokens != null &&
                            ` · ${m.usage_json.completion_tokens} tok`}
                          {m.usage_json?.completion_tokens != null &&
                            m.latency_ms != null &&
                            m.latency_ms > 0 &&
                            ` · ${(
                              m.usage_json.completion_tokens /
                              (m.latency_ms / 1000)
                            ).toFixed(1)} tok/s`}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}

            {live &&
              live.status !== "error" &&
              !(live.turnId && orderedTurns.some((t) => t.id === live.turnId)) && (
              <div>
                {(live.statusText ||
                  live.status === "streaming" ||
                  live.status === "queued") && (
                  <div className="mb-1 flex items-center gap-1 text-xs text-blue-500">
                    <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-blue-500" />
                    {live.statusText ||
                      (live.status === "queued"
                        ? "Queued…"
                        : "Request sent · awaiting response…")}
                  </div>
                )}
                {live.toolCalls.map((tc) => (
                  <ToolCallCard key={tc.tool_call_id} call={tc} />
                ))}
                <MessageRenderer content={live.content || "…"} />
              </div>
            )}
            {live?.status === "error" && (
              <div className="text-xs text-red-500">Error: {live.error}</div>
            )}
            </div>
            </CodeFoldContext.Provider>
          </div>

          {queued && queued.length > 0 && (
            <div className="space-y-1 border-t border-amber-200 bg-amber-50 px-2 py-1 dark:border-amber-900/40 dark:bg-amber-950/30">
              {queued.map((q) => (
                <div key={q.id} className="flex items-center gap-1.5 text-xs">
                  <span className="shrink-0 rounded bg-amber-400/90 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide text-amber-900">
                    Queued
                  </span>
                  <span
                    className="min-w-0 flex-1 truncate text-amber-900 dark:text-amber-200"
                    title={q.content}
                  >
                    {q.content || "(attachment)"}
                  </span>
                  {onSendQueuedNow && (
                    <button
                      onClick={() => onSendQueuedNow(q.id)}
                      title="Send to this lane now (interrupts the current response)"
                      className="shrink-0 rounded px-1 text-amber-700 hover:bg-amber-200 dark:text-amber-300 dark:hover:bg-amber-900/50"
                    >
                      ⚡ Send now
                    </button>
                  )}
                  {onRemoveQueued && (
                    <button
                      onClick={() => onRemoveQueued(q.id)}
                      title="Remove from queue"
                      className="shrink-0 rounded px-1 text-amber-600 hover:bg-amber-200 hover:text-red-500 dark:hover:bg-amber-900/50"
                    >
                      ✕
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}

          <div className="flex flex-wrap items-center gap-1 border-t border-gray-200 px-2 py-1 dark:border-gray-700">
            {status === "streaming" ? (
              <button
                onClick={onStop}
                className="rounded bg-red-100 px-2 py-0.5 text-xs text-red-700"
              >
                Stop
              </button>
            ) : (
              <button
                onClick={onRegenerate}
                disabled={status === "queued"}
                className="rounded bg-gray-100 px-2 py-0.5 text-xs disabled:opacity-40 dark:bg-gray-800"
              >
                {status === "error" ? "Retry" : "Regenerate"}
              </button>
            )}
            <button
              onClick={onPickBest}
              className="rounded bg-gray-100 px-2 py-0.5 text-xs dark:bg-gray-800"
            >
              {isBest ? "Unstar" : "Best"}
            </button>
            {onContinue && (
              <button
                onClick={onContinue}
                title="Continue this model in a focused new chat"
                className="rounded bg-gray-100 px-2 py-0.5 text-xs dark:bg-gray-800"
              >
                → Continue
              </button>
            )}
            <button
              onClick={onRemove}
              className="ml-auto rounded px-2 py-0.5 text-xs text-gray-400 hover:text-red-500"
            >
              Remove
            </button>
          </div>

          {lane.role === "responder" && onSendToLane && (() => {
            const laneBusy =
              status === "streaming" ||
              status === "queued" ||
              status === "thinking";
            return (
            <div className="flex items-center gap-1 border-t border-gray-200 px-2 py-1 dark:border-gray-700">
              <input
                value={laneDraft}
                data-lane-input={lane.id}
                onChange={(e) => setLaneDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey && !laneBusy) {
                    e.preventDefault();
                    sendLaneDraft();
                  }
                }}
                placeholder={
                  laneBusy
                    ? `${lane.model} is responding…`
                    : `Message ${lane.model} only…`
                }
                title={`Send a message to ${lane.model} only`}
                className="min-w-0 flex-1 rounded border border-gray-300 px-2 py-1 text-xs dark:border-gray-600 dark:bg-gray-800"
              />
              <button
                onClick={sendLaneDraft}
                disabled={!laneDraft.trim() || laneBusy}
                title={
                  laneBusy
                    ? "Waiting for this lane to finish…"
                    : `Send only to ${lane.model}`
                }
                className="flex shrink-0 items-center gap-1 rounded bg-brand px-2 py-1 text-xs font-medium text-white hover:brightness-110 disabled:opacity-40"
              >
                {laneBusy && (
                  <svg
                    className="h-3 w-3 animate-spin"
                    viewBox="0 0 24 24"
                    fill="none"
                    aria-hidden="true"
                  >
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
                Send
              </button>
            </div>
            );
          })()}
        </>
      )}
    </div>
  );
}
