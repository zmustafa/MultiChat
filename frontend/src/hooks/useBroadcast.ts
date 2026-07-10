import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch, streamSSE } from "../api/client";

export interface LiveToolCall {
  tool_call_id: string;
  tool: string;
  arguments: Record<string, any>;
  status: string;
  result?: string;
  citations?: any[] | null;
}

export interface LiveLane {
  laneId: string;
  turnId?: string;
  status: "queued" | "streaming" | "done" | "error";
  content: string;
  toolCalls: LiveToolCall[];
  statusText?: string;
  latencyMs?: number;
  ttftMs?: number;
  costUsd?: number;
  usage?: Record<string, any>;
  error?: string;
}

export type LiveMap = Record<string, LiveLane>;

export function useBroadcast(sessionId: string | null, onComplete?: () => void) {
  const [live, setLive] = useState<LiveMap>({});
  // Track each in-flight stream by key so multiple lanes can regenerate concurrently
  // and independently (a broadcast uses one key; each single-lane regenerate its own).
  const [activeKeys, setActiveKeys] = useState<Set<string>>(new Set());
  const controllersRef = useRef<Map<string, AbortController>>(new Map());
  const liveRef = useRef<LiveMap>(live);
  liveRef.current = live;
  const streaming = activeKeys.size > 0;

  // Chunk coalescing buffers (see flushChunks below): accumulate per-lane token deltas and
  // flush them in one state update per animation frame instead of one setLive per token.
  const chunkBufRef = useRef<Record<string, string>>({});
  const rafRef = useRef<number | null>(null);

  // When the active session changes (switching chats or opening a brand-new chat), tear
  // down any client-side streams from the previous session and clear their state. The
  // SSE streams are session-specific (the backend persists partial answers, and the
  // reconcile effect recovers on load), so leftover keys must not linger — otherwise a
  // stuck key keeps `streaming` true and the composer wrongly shows the queue popup on a
  // fresh chat where nothing is generating.
  useEffect(() => {
    controllersRef.current.forEach((c) => c.abort());
    controllersRef.current.clear();
    setActiveKeys(new Set());
    setLive({});
    if (rafRef.current != null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
    chunkBufRef.current = {};
  }, [sessionId]);

  // Chunk coalescing: token chunks can arrive dozens of times per second per lane. Applying
  // each one as its own setLive re-renders the whole compare grid every token. Instead we
  // buffer incoming deltas per lane and flush them all in ONE state update per animation
  // frame — cutting streaming re-renders by an order of magnitude while looking identical.
  const flushChunks = useCallback(() => {
    if (rafRef.current != null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
    const buf = chunkBufRef.current;
    const ids = Object.keys(buf);
    if (ids.length === 0) return;
    chunkBufRef.current = {};
    setLive((prev) => {
      const next = { ...prev };
      for (const id of ids) {
        const cur =
          next[id] ||
          ({ laneId: id, status: "streaming", content: "", toolCalls: [] } as LiveLane);
        next[id] = { ...cur, content: cur.content + buf[id] };
      }
      return next;
    });
  }, []);

  const update = useCallback(
    (laneId: string, patch: Partial<LiveLane>) => {
      setLive((prev) => {
        const cur =
          prev[laneId] ||
          ({
            laneId,
            status: "queued",
            content: "",
            toolCalls: [],
          } as LiveLane);
        return { ...prev, [laneId]: { ...cur, ...patch } };
      });
    },
    []
  );

  const handleEvent = useCallback(
    (evt: { event: string; data: any }) => {
      const d = evt.data;
      // Any non-chunk event (tool call/result, status, done) must see the buffered text
      // already applied, so flush pending chunks before handling it.
      if (evt.event !== "chunk") flushChunks();
      switch (evt.event) {
        case "lane_start":
          update(d.lane_id, {
            status: "streaming",
            content: "",
            toolCalls: [],
            statusText: "Request sent · awaiting response…",
            turnId: d.turn_id,
          });
          break;
        case "lane_status":
          update(d.lane_id, { statusText: d.text });
          break;
        case "chunk":
          chunkBufRef.current[d.lane_id] =
            (chunkBufRef.current[d.lane_id] || "") + d.delta;
          if (rafRef.current == null) {
            rafRef.current = requestAnimationFrame(flushChunks);
          }
          break;
        case "tool_call":
          setLive((prev) => {
            const cur = prev[d.lane_id];
            if (!cur) return prev;
            return {
              ...prev,
              [d.lane_id]: {
                ...cur,
                toolCalls: [
                  ...cur.toolCalls,
                  {
                    tool_call_id: d.tool_call_id,
                    tool: d.tool,
                    arguments: d.arguments,
                    status: "running",
                  },
                ],
              },
            };
          });
          break;
        case "tool_result":
          setLive((prev) => {
            const cur = prev[d.lane_id];
            if (!cur) return prev;
            return {
              ...prev,
              [d.lane_id]: {
                ...cur,
                toolCalls: cur.toolCalls.map((tc) =>
                  tc.tool_call_id === d.tool_call_id
                    ? { ...tc, status: d.status, result: d.result, citations: d.citations }
                    : tc
                ),
              },
            };
          });
          break;
        case "lane_done": {
          // Adopt the server's final content as authoritative: the backend reconciles
          // download links (rewriting fabricated/omitted file links to the real
          // /api/files/ URLs) after streaming, so the streamed text may be out of date.
          const patch: Partial<LiveLane> = {
            status: "done",
            latencyMs: d.latency_ms,
            ttftMs: d.ttft_ms,
            costUsd: d.cost_usd,
            usage: d.usage,
          };
          if (typeof d.message?.content === "string") {
            patch.content = d.message.content;
          }
          update(d.lane_id, patch);
          break;
        }
        case "lane_error":
          // Ignore empty-detail errors — these are abort artifacts from a stream that
          // was interrupted (the user sent a new message); they'd otherwise flash a
          // meaningless "Error:" over the freshly started response.
          if (d.detail) update(d.lane_id, { status: "error", error: d.detail });
          break;
        case "done":
          break;
      }
    },
    [update, flushChunks]
  );

  const runStream = useCallback(
    (key: string, path: string, body: unknown) => {
      // If a stream with the same key is already running (e.g. re-regenerating the
      // same lane), abort it first so we don't double-stream that lane.
      controllersRef.current.get(key)?.abort();
      setActiveKeys((prev) => new Set(prev).add(key));
      const finish = () => {
        controllersRef.current.delete(key);
        setActiveKeys((prev) => {
          if (!prev.has(key)) return prev;
          const next = new Set(prev);
          next.delete(key);
          return next;
        });
        onComplete?.();
      };
      const ctrl = streamSSE(path, body, handleEvent, finish, finish);
      controllersRef.current.set(key, ctrl);
    },
    [handleEvent, onComplete]
  );

  const broadcast = useCallback(
    (
      content: string,
      attachmentIds?: string[],
      targetLaneIds?: string[],
      key = "broadcast"
    ) => {
      if (!sessionId) return;
      runStream(key, `/api/sessions/${sessionId}/broadcast`, {
        content,
        attachment_ids: attachmentIds,
        target_lane_ids: targetLaneIds,
      });
    },
    [sessionId, runStream]
  );

  const regenerate = useCallback(
    (laneId: string, turnId?: string) => {
      if (!sessionId) return;
      // Show an immediate "awaiting" status (like the first message) so the lane
      // reacts the instant the button/icon is clicked, before the first SSE event.
      update(laneId, {
        status: "streaming",
        statusText: "Request sent · awaiting response…",
        content: "",
        toolCalls: [],
        error: undefined,
        turnId,
      });
      runStream(
        `lane:${laneId}`,
        `/api/sessions/${sessionId}/lanes/${laneId}/regenerate`,
        { turn_id: turnId }
      );
    },
    [sessionId, runStream, update]
  );

  const judge = useCallback(
    (turnId: string) => {
      if (!sessionId) return;
      runStream("judge", `/api/sessions/${sessionId}/judge`, { turn_id: turnId });
    },
    [sessionId, runStream]
  );

  // Re-attach to an in-flight broadcast turn: the backend replays the SSE events emitted
  // so far, then tails new ones live, so a client returning to a chat whose lanes are
  // still generating streams token-by-token instead of only polling partial text.
  const resume = useCallback(
    (turnId: string) => {
      if (!sessionId) return;
      runStream(`resume:${turnId}`, `/api/sessions/${sessionId}/resume`, {
        turn_id: turnId,
      });
    },
    [sessionId, runStream]
  );

  const stopLane = useCallback(
    async (laneId: string) => {
      if (!sessionId) return;
      // Give immediate feedback: the backend cancel + final `lane_done` round-trip can
      // take a moment, and without this the button appears to do nothing.
      if (liveRef.current[laneId]?.status === "streaming") {
        update(laneId, { statusText: "Stopping…" });
      }
      await apiFetch<void>(`/api/sessions/${sessionId}/lanes/${laneId}/stop`, {
        method: "POST",
      }).catch(() => undefined);
    },
    [sessionId, update]
  );

  const cancelAll = useCallback(() => {
    controllersRef.current.forEach((c) => c.abort());
    controllersRef.current.clear();
    setActiveKeys(new Set());
  }, []);

  /**
   * Interrupt every in-flight stream: tell the backend to cancel each active lane
   * (it persists the partial answer generated so far) and stop listening client-side
   * so late events from the old streams don't corrupt the next request's state.
   * Used when the user sends a new message while a response is still streaming.
   */
  const stopAll = useCallback(async () => {
    const active = Object.values(liveRef.current)
      .filter((l) => l.status === "streaming" || l.status === "queued")
      .map((l) => l.laneId);
    controllersRef.current.forEach((c) => c.abort());
    controllersRef.current.clear();
    setActiveKeys(new Set());
    if (!sessionId) return;
    await Promise.all(
      active.map((id) =>
        apiFetch<void>(`/api/sessions/${sessionId}/lanes/${id}/stop`, {
          method: "POST",
        }).catch(() => undefined)
      )
    );
  }, [sessionId]);

  const clearLive = useCallback(() => setLive({}), []);
  const clearLane = useCallback(
    (laneId: string) =>
      setLive((prev) => {
        if (!prev[laneId]) return prev;
        const next = { ...prev };
        delete next[laneId];
        return next;
      }),
    []
  );

  return {
    live,
    streaming,
    broadcast,
    regenerate,
    judge,
    resume,
    stopLane,
    stopAll,
    cancelAll,
    clearLive,
    clearLane,
  };
}
