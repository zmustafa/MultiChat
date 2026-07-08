import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { API_BASE, apiFetch, getToken, mediaUrl } from "../api/client";
import type { LaneRole, Persona } from "../api/types";
import { CommandPalette } from "../components/CommandPalette";
import { ArtifactPanel } from "../components/ArtifactPanel";
import { CompareGrid } from "../components/CompareGrid";
import { DiffView } from "../components/DiffView";
import { InsightsPanel } from "../components/InsightsPanel";
import { FilesPanel } from "../components/FilesPanel";
import { SnapshotsPanel } from "../components/SnapshotsPanel";
import { JudgePanel } from "../components/JudgePanel";
import { LaneComposer } from "../components/LaneComposer";
import type { QueuedMessage } from "../components/LaneComposer";
import { ModelPicker } from "../components/ModelPicker";
import { SessionSidebar } from "../components/SessionSidebar";
import { ThemeToggle } from "../components/ThemeToggle";
import { useAuth } from "../auth/AuthContext";
import { useBroadcast } from "../hooks/useBroadcast";
import type { LiveLane } from "../hooks/useBroadcast";
import { useDismiss } from "../hooks/useDismiss";
import { usePersonas, usePersonaMutations } from "../hooks/usePersonas";
import { useProviders } from "../hooks/useProviders";
import { useTheme } from "../hooks/useTheme";
import { seedLaneCollapse } from "../utils/laneCollapse";
import {
  useActiveSessions,
  useSession,
  useSessionMutations,
  useSessions,
} from "../hooks/useSessions";

const ALL_TOOLS = [
  "web_search",
  "fetch_url",
  "calculator",
  "current_date",
  "read_document",
  "generate_image",
  "generate_pptx",
  "generate_docx",
  "generate_xlsx",
  "generate_pdf",
];

export function ComparePage() {
  const { logout, user } = useAuth();
  const qc = useQueryClient();
  const nav = useNavigate();
  const theme = useTheme();
  const { data: sessions = [] } = useSessions();
  const { data: providers = [] } = useProviders();
  const { data: personas = [] } = usePersonas();
  const personaMut = usePersonaMutations();
  // The active chat is identified by the URL (/c/:sessionId), so every chat has a
  // permanent, shareable/bookmarkable link. localStorage only remembers the last
  // chat to restore when landing on "/".
  const { sessionId } = useParams<{ sessionId: string }>();
  const activeId = sessionId ?? null;
  const setActiveId = useCallback(
    (id: string | null) => {
      nav(id ? `/c/${id}` : "/");
    },
    [nav],
  );
  const { data: session } = useSession(activeId);
  const sm = useSessionMutations();
  const [showDiff, setShowDiff] = useState(false);
  const [editDraft, setEditDraft] = useState<{ text: string; ts: number } | null>(null);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [toolsOpen, setToolsOpen] = useState(false);
  const [showArtifacts, setShowArtifacts] = useState(false);
  const [promptOpen, setPromptOpen] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
  const moreRef = useRef<HTMLDivElement>(null);
  useDismiss(moreRef, moreOpen, () => setMoreOpen(false));
  const [showInsights, setShowInsights] = useState(false);
  const [showFiles, setShowFiles] = useState(false);
  const [showSnapshots, setShowSnapshots] = useState(false);
  const [density, setDensity] = useState<"comfortable" | "compact">(
    () => (localStorage.getItem("multichat_density") as "comfortable" | "compact") || "comfortable"
  );
  const [fitToScreen, setFitToScreen] = useState(
    () => localStorage.getItem("multichat_fit") === "1"
  );
  useEffect(() => {
    localStorage.setItem("multichat_density", density);
  }, [density]);
  useEffect(() => {
    localStorage.setItem("multichat_fit", fitToScreen ? "1" : "0");
  }, [fitToScreen]);

  // Queued messages (VS Code-style): dispatched to each lane as it frees up.
  const [queue, setQueue] = useState<QueuedMessage[]>([]);
  const queueRef = useRef<QueuedMessage[]>(queue);
  queueRef.current = queue;
  // Per-message set of lanes already dispatched to (so we never double-send).
  const servedRef = useRef<Record<string, Set<string>>>({});
  const waveRef = useRef(0);
  // Lanes we've just dispatched a queued message to but whose stream hasn't started yet.
  // They still count as "busy" so the gap before lane_start can't hand them the NEXT
  // queued message (which would run two responses in the same lane at once).
  const dispatchingRef = useRef<Set<string>>(new Set());
  // Reactive mirror of servedRef, used only to render the per-lane queued bars (so a
  // lane's bar disappears once the message has been dispatched to it).
  const [servedView, setServedView] = useState<Record<string, string[]>>({});
  // Clear the queue when switching chats.
  useEffect(() => {
    setQueue([]);
    servedRef.current = {};
    dispatchingRef.current = new Set();
    setServedView({});
  }, [activeId]);
  const [navCollapsed, setNavCollapsed] = useState(
    () => localStorage.getItem("multichat_nav_collapsed") === "1"
  );
  useEffect(() => {
    localStorage.setItem("multichat_nav_collapsed", navCollapsed ? "1" : "0");
  }, [navCollapsed]);

  // Global Ctrl/Cmd-K opens the command palette.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((o) => !o);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const [bestLane, setBestLane] = useState<Record<string, string>>(() => {
    try {
      return JSON.parse(localStorage.getItem("multichat_best") || "{}");
    } catch {
      return {};
    }
  });

  // Custom per-lane column widths (px). Empty entry = auto/balanced (flex).
  const [laneWidths, setLaneWidths] = useState<Record<string, number>>(() => {
    try {
      return JSON.parse(localStorage.getItem("multichat_lane_widths") || "{}");
    } catch {
      return {};
    }
  });
  const setLaneWidth = useCallback((laneId: string, width: number) => {
    setLaneWidths((prev) => {
      const next = { ...prev };
      if (!width) delete next[laneId];
      else next[laneId] = width;
      localStorage.setItem("multichat_lane_widths", JSON.stringify(next));
      return next;
    });
  }, []);
  const resetLayout = useCallback(() => {
    setLaneWidths({});
    localStorage.setItem("multichat_lane_widths", "{}");
  }, []);

  useEffect(() => {
    if (activeId && session) localStorage.setItem("multichat_active", activeId);
  }, [activeId, session]);

  // When landing on "/" with no chat selected, restore the last-opened chat (if any)
  // so its permanent link is reflected in the URL.
  useEffect(() => {
    if (sessionId) return;
    const last = localStorage.getItem("multichat_active");
    if (last) nav(`/c/${last}`, { replace: true });
  }, [sessionId, nav]);

  const refresh = () => {
    if (activeId) qc.invalidateQueries({ queryKey: ["session", activeId] });
    qc.invalidateQueries({ queryKey: ["sessions"] });
  };

  const { live, streaming, broadcast, regenerate, judge, resume, stopLane, stopAll, clearLive, clearLane } =
    useBroadcast(activeId, () => {
      // Only refetch persisted state. Each lane's live overlay auto-hides once its
      // persisted message arrives, so we must NOT clear all live state here — that
      // would wipe other lanes still streaming (independent regenerations).
      refresh();
    });

  // Whether any lane is ACTUALLY still generating right now. `streaming` (from useBroadcast)
  // is true whenever an SSE stream is open, which lingers in the gap between every lane
  // finishing and the stream closing — so it can't be trusted to decide the composer's
  // "a response is still generating" popup. The live map, by contrast, flips each lane to
  // "done" the instant its lane_done event arrives (unlike the React-Query lane state,
  // which lags until a refetch), so it reflects real generation state immediately. Once
  // all lanes are done, sending fires immediately instead of showing the popup.
  const anyLaneBusy = useMemo(
    () =>
      Object.values(live).some(
        (l) => l.status === "streaming" || l.status === "queued",
      ),
    [live],
  );

  // Which chats currently have a lane generating (active or background), for the sidebar
  // spinner. The poll is authoritative; we also fold in the active chat's local live state
  // so its spinner appears/disappears instantly rather than waiting for the next poll.
  const { data: activeSessions } = useActiveSessions();
  const generatingIds = useMemo(() => {
    const s = new Set(activeSessions?.session_ids ?? []);
    if (activeId && anyLaneBusy) s.add(activeId);
    return s;
  }, [activeSessions, activeId, anyLaneBusy]);

  // Global keyboard shortcuts: `/` focuses the prompt box, Alt+1..9 focuses a lane's inline
  // input, and Esc stops all in-flight responses. (Ctrl+K palette + Ctrl/⌘+Enter send are
  // handled elsewhere: the palette hotkey and the composer's Enter-to-send.)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      const typing =
        !!t &&
        (t.tagName === "INPUT" ||
          t.tagName === "TEXTAREA" ||
          t.isContentEditable);
      if (
        e.key === "/" &&
        !typing &&
        !e.metaKey &&
        !e.ctrlKey &&
        !e.altKey
      ) {
        e.preventDefault();
        document.querySelector<HTMLTextAreaElement>("textarea")?.focus();
        return;
      }
      if (e.altKey && /^[1-9]$/.test(e.key)) {
        const responders =
          session?.lanes.filter((l) => l.role === "responder") ?? [];
        const lane = responders[parseInt(e.key, 10) - 1];
        if (lane) {
          e.preventDefault();
          const el = document.querySelector<HTMLInputElement>(
            `[data-lane-input="${lane.id}"]`,
          );
          el?.scrollIntoView({ block: "nearest" });
          el?.focus();
        }
        return;
      }
      if (e.key === "Escape" && anyLaneBusy) {
        e.preventDefault();
        stopAll();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [session, anyLaneBusy, stopAll]);

  // Reconcile lanes left stuck in "streaming" by a dropped connection or backend
  // reload: if a lane shows streaming but isn't actually generating (not in the
  // server's active-run list and not in our local live overlay), reset it to idle.
  useEffect(() => {
    if (!activeId || !session) return;
    const stuck = session.lanes.filter((l) => l.state === "streaming" && !live[l.id]);
    if (stuck.length === 0) return;
    let cancelled = false;
    apiFetch<{ running: string[] }>(`/api/sessions/${activeId}/runs`)
      .then((r) => {
        if (cancelled) return;
        const running = new Set(r.running);
        stuck.forEach((l) => {
          if (!running.has(l.id))
            sm.updateLane.mutate({ id: activeId, laneId: l.id, body: { state: "idle" } });
        });
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId, session?.id, session?.lanes.map((l) => l.state).join(",")]);

  // If we return to a chat whose lanes are still generating in the BACKGROUND (the
  // response kept running after we navigated away — the backend detaches, rather than
  // cancels, lanes on disconnect), there is no live SSE stream to update them. Poll the
  // backend's in-memory progress so the partial answer grows on screen (instead of a blank
  // spinner), and refetch the session to pick up each lane's final message once it drops
  // out of progress. Stops once no lane is "streaming" without a local live overlay.
  const [bgProgress, setBgProgress] = useState<Record<string, LiveLane>>({});
  const bgPrevRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (!activeId || !session) return;
    const background = session.lanes.some(
      (l) => l.role === "responder" && l.state === "streaming" && !live[l.id],
    );
    if (!background) {
      if (Object.keys(bgProgress).length) setBgProgress({});
      bgPrevRef.current = new Set();
      return;
    }
    let cancelled = false;
    const poll = async () => {
      try {
        const r = await apiFetch<{
          lanes: { lane_id: string; turn_id: string; text: string }[];
        }>(`/api/sessions/${activeId}/progress`);
        if (cancelled) return;
        const next: Record<string, LiveLane> = {};
        const now = new Set<string>();
        for (const p of r.lanes) {
          now.add(p.lane_id);
          next[p.lane_id] = {
            laneId: p.lane_id,
            turnId: p.turn_id,
            status: "streaming",
            content: p.text,
            toolCalls: [],
            statusText: "Generating (in the background)…",
          };
        }
        setBgProgress(next);
        // A lane that WAS generating but is no longer in progress just finished — refetch
        // the session so its persisted answer + footer replace the progress overlay.
        let finished = false;
        for (const id of bgPrevRef.current) if (!now.has(id)) finished = true;
        bgPrevRef.current = now;
        if (finished) qc.invalidateQueries({ queryKey: ["session", activeId] });
      } catch {
        /* ignore transient poll errors */
      }
    };
    poll();
    const iv = setInterval(poll, 1500);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId, session?.lanes.map((l) => l.state).join(","), live, qc]);

  // Merge background progress under the real live map (real SSE always wins) so lanes
  // running in the background render their growing partial answer.
  const mergedLive = useMemo(
    () => ({ ...bgProgress, ...live }),
    [bgProgress, live],
  );

  // Re-attach LIVE to background runs. The progress poll above tells us which turn(s) are
  // still generating; for each, open a resume SSE stream so the answer streams
  // token-by-token (the backend replays what was already emitted, then tails the rest)
  // instead of only refreshing every 1.5s. Once resume populates the real `live` map the
  // poll condition turns off on its own. If the run's in-memory buffer is gone (server
  // restarted / TTL elapsed) the resume stream is empty and the poll remains the fallback.
  const resumedTurnsRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    // Forget resumed turns when switching chats so a new chat can resume its own runs.
    resumedTurnsRef.current = new Set();
  }, [activeId]);
  useEffect(() => {
    for (const l of Object.values(bgProgress)) {
      if (l.turnId && !resumedTurnsRef.current.has(l.turnId)) {
        resumedTurnsRef.current.add(l.turnId);
        resume(l.turnId);
      }
    }
  }, [bgProgress, resume]);

  // Show the blue "You" prompt immediately when a response starts streaming, and pull in
  // each lane's persisted message (with its footer: copy/pin/regenerate + latency/tokens)
  // as soon as THAT lane finishes — without waiting for the slower lanes. A broadcast is a
  // single SSE stream that only ends (triggering the completion refetch) once EVERY lane is
  // done, so we refetch on the first sight of a new turn id and on each lane's "done".
  const seenTurnsRef = useRef<Set<string>>(new Set());
  const doneRef = useRef<Set<string>>(new Set());
  // Debounce session refetches: when several lanes finish within a few ms of each other
  // (a broadcast completing), we'd otherwise fire a full session refetch per lane. Coalesce
  // them into one refetch shortly after the burst settles.
  const refetchTimerRef = useRef<number | null>(null);
  const scheduleRefetch = useCallback(() => {
    if (!activeId) return;
    if (refetchTimerRef.current != null) clearTimeout(refetchTimerRef.current);
    refetchTimerRef.current = window.setTimeout(() => {
      refetchTimerRef.current = null;
      qc.invalidateQueries({ queryKey: ["session", activeId] });
    }, 250);
  }, [activeId, qc]);
  useEffect(() => {
    if (!activeId || !session) return;
    const known = new Set(session.turns.map((t) => t.id));
    let refetch = false;
    for (const l of Object.values(live)) {
      if (l.turnId && !known.has(l.turnId) && !seenTurnsRef.current.has(l.turnId)) {
        seenTurnsRef.current.add(l.turnId);
        refetch = true;
      }
      if (l.status === "done" && l.turnId) {
        const key = `${l.laneId}:${l.turnId}`;
        if (!doneRef.current.has(key)) {
          doneRef.current.add(key);
          refetch = true;
        }
      }
    }
    if (refetch) scheduleRefetch();
  }, [live, session, activeId, qc, scheduleRefetch]);

  // Drain the queue PER-LANE and IN ORDER: each free lane picks up the EARLIEST queued
  // message that targets it and hasn't been sent to it yet — independently of the other
  // lanes. This handles multiple queued messages: a fast lane can work through msg1 then
  // msg2 while a slow lane is still on msg1. `servedRef` tracks which lanes each message
  // has been dispatched to; a message leaves the queue once all its targets are served.
  useEffect(() => {
    if (!activeId) return;
    const q = queueRef.current;
    // Garbage-collect served-lane sets for messages that have left the queue. We must
    // NOT delete a message's served set until it is truly gone from the queue, or a
    // re-run of this effect (before setQueue commits) could re-dispatch to a lane that
    // was already served — producing a duplicate response in that lane.
    let gcChanged = false;
    for (const id of Object.keys(servedRef.current)) {
      if (!q.some((m) => m.id === id)) {
        delete servedRef.current[id];
        gcChanged = true;
      }
    }
    if (gcChanged) {
      setServedView((prev) => {
        const next: Record<string, string[]> = {};
        for (const id of Object.keys(prev)) {
          if (q.some((m) => m.id === id)) next[id] = prev[id];
        }
        return next;
      });
    }
    if (q.length === 0) return;
    const responders = (session?.lanes || []).filter((l) => l.role === "responder");
    if (responders.length === 0) return;

    // A lane whose dispatched stream has actually started is now governed by its live
    // status; drop it from the "just dispatched" set so it can queue up its next message.
    for (const id of Array.from(dispatchingRef.current)) {
      const st = live[id]?.status;
      if (st === "streaming" || st === "done" || st === "error") {
        dispatchingRef.current.delete(id);
      }
    }
    const busy = (id: string) => {
      if (dispatchingRef.current.has(id)) return true;
      const s = live[id]?.status;
      return s === "streaming" || s === "queued";
    };
    const targetsOf = (m: QueuedMessage) =>
      m.targetLaneIds && m.targetLaneIds.length
        ? m.targetLaneIds.filter((id) => responders.some((l) => l.id === id))
        : responders.map((l) => l.id);

    // For each free lane, find the earliest queued message it still needs.
    const pickByLane: Record<string, QueuedMessage> = {};
    for (const lane of responders) {
      if (busy(lane.id)) continue;
      for (const m of q) {
        if (!targetsOf(m).includes(lane.id)) continue;
        if (servedRef.current[m.id]?.has(lane.id)) continue;
        pickByLane[lane.id] = m;
        break;
      }
    }

    // Group the picked lanes by message so each message dispatched this tick goes out as
    // a single broadcast (one turn) targeting the set of lanes that picked it.
    const byMsg = new Map<string, { msg: QueuedMessage; lanes: string[] }>();
    for (const [laneId, m] of Object.entries(pickByLane)) {
      const entry = byMsg.get(m.id) || { msg: m, lanes: [] };
      entry.lanes.push(laneId);
      byMsg.set(m.id, entry);
    }
    for (const { msg, lanes } of byMsg.values()) {
      let served = servedRef.current[msg.id];
      if (!served) {
        served = new Set<string>();
        servedRef.current[msg.id] = served;
      }
      lanes.forEach((id) => {
        served!.add(id);
        dispatchingRef.current.add(id);
        clearLane(id);
      });
      broadcast(
        msg.content,
        msg.attachments.map((a) => a.id),
        lanes,
        `qsend:${msg.id}:${waveRef.current++}`
      );
      setServedView((prev) => ({ ...prev, [msg.id]: Array.from(served!) }));
    }

    // Remove messages that have been served to every target (or target no valid lane).
    const drop = new Set<string>();
    for (const m of q) {
      const tids = targetsOf(m);
      if (tids.length === 0) {
        drop.add(m.id);
        continue;
      }
      const served = servedRef.current[m.id];
      if (served && tids.every((id) => served.has(id))) drop.add(m.id);
    }
    if (drop.size > 0) {
      // Do NOT delete servedRef here; the top-of-effect GC removes it only once the
      // message is actually gone from the queue, which prevents a duplicate dispatch
      // during the window before setQueue commits.
      setQueue((prev) => prev.filter((m) => !drop.has(m.id)));
    }
  }, [live, activeId, queue, session?.lanes, broadcast, clearLane]);

  // Queued messages still pending for each lane (targets it and not yet dispatched to
  // it), so the per-lane queued bars know what to show.
  const queuedByLane = useMemo(() => {
    const responders = (session?.lanes || []).filter((l) => l.role === "responder");
    const map: Record<string, QueuedMessage[]> = {};
    for (const l of responders) map[l.id] = [];
    for (const m of queue) {
      const targets =
        m.targetLaneIds && m.targetLaneIds.length
          ? m.targetLaneIds.filter((id) => responders.some((l) => l.id === id))
          : responders.map((l) => l.id);
      const servedIds = servedView[m.id] || [];
      for (const id of targets) {
        if (!servedIds.includes(id) && map[id]) map[id].push(m);
      }
    }
    return map;
  }, [queue, session?.lanes, servedView]);

  // "Send now" for a single lane: dispatch a queued message to just that lane
  // immediately, interrupting whatever it is currently streaming.
  const sendQueuedNowToLane = useCallback(
    async (msgId: string, laneId: string) => {
      const msg = queueRef.current.find((m) => m.id === msgId);
      if (!msg) return;
      let served = servedRef.current[msg.id];
      if (!served) {
        served = new Set<string>();
        servedRef.current[msg.id] = served;
      }
      if (served.has(laneId)) return;
      // Reserve the lane first so the background drain can't also dispatch to it.
      served.add(laneId);
      // Mark it "dispatching" so the drain treats it as busy until its stream starts and
      // can't slip an earlier queued message into the gap before lane_start.
      dispatchingRef.current.add(laneId);
      setServedView((prev) => ({ ...prev, [msg.id]: Array.from(served!) }));
      if (live[laneId]?.status === "streaming") await stopLane(laneId);
      clearLane(laneId);
      broadcast(
        msg.content,
        msg.attachments.map((a) => a.id),
        [laneId],
        `qnow:${msg.id}:${waveRef.current++}`
      );
      const responders = (session?.lanes || []).filter((l) => l.role === "responder");
      const targetIds =
        msg.targetLaneIds && msg.targetLaneIds.length
          ? msg.targetLaneIds.filter((id) => responders.some((l) => l.id === id))
          : responders.map((l) => l.id);
      if (targetIds.every((id) => served!.has(id))) {
        setQueue((prev) => prev.filter((m) => m.id !== msg.id));
      }
    },
    [live, session?.lanes, broadcast, clearLane, stopLane]
  );
  const removeQueued = useCallback(
    (msgId: string) => setQueue((prev) => prev.filter((m) => m.id !== msgId)),
    []
  );

  const latestTurn = useMemo(() => {
    if (!session?.turns.length) return undefined;
    return [...session.turns].sort((a, b) => b.order_index - a.order_index)[0];
  }, [session]);

  const judgeLane = session?.lanes.find((l) => l.role === "judge");

  // Auto-generate a short AI title (via the default provider) for a "New topic" chat
  // once it has its first exchange. Runs once per session.
  const titledRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (!session || !activeId || streaming) return;
    // A chat still carrying its initial title — "New topic", empty, or the persona name it
    // was created from — should get an AI-generated title after its first exchange. A title
    // the user has manually changed to something else is left alone.
    const isDefaultTitle =
      !session.title ||
      session.title === "New topic" ||
      personas.some((p) => p.name === session.title);
    const hasExchange =
      session.turns.length >= 1 &&
      session.messages.some((m) => m.role === "assistant" && m.content);
    if (isDefaultTitle && hasExchange && !titledRef.current.has(activeId)) {
      titledRef.current.add(activeId);
      sm.autotitle.mutate(activeId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session, streaming, activeId, personas]);

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
      setActiveId(created.id);
      return;
    }
    const created = await sm.create.mutateAsync({ title: "New topic", lanes: [] });
    setActiveId(created.id);
  }

  async function saveAsPersona() {
    if (!session) return;
    const name = prompt("Persona name (e.g. Cloud Architect):", session.title);
    if (!name) return;
    await personaMut.create.mutateAsync({
      name,
      system_prompt: session.system_prompt || null,
      tools_enabled: session.tools_enabled,
      lanes: session.lanes
        .filter((l) => l.role === "responder" || l.role === "judge")
        .map((l) => ({ provider_id: l.provider_id, model: l.model, role: l.role })),
    });
    alert(`Saved persona “${name}” with ${session.lanes.length} lane(s).`);
  }

  async function addLane(provider_id: string, model: string, role: LaneRole) {
    if (!activeId) {
      const created = await sm.create.mutateAsync({
        title: "New topic",
        lanes: [{ provider_id, model, role }],
      });
      setActiveId(created.id);
      return;
    }
    await sm.addLane.mutateAsync({ id: activeId, body: { provider_id, model, role } });
  }

  function setBest(laneId: string) {
    if (!activeId) return;
    setBestLane((prev) => {
      const next = { ...prev };
      if (next[activeId] === laneId) delete next[activeId];
      else next[activeId] = laneId;
      localStorage.setItem("multichat_best", JSON.stringify(next));
      return next;
    });
  }

  function regenerateAll() {
    if (!session || !latestTurn) return;
    session.lanes
      .filter((l) => l.role === "responder")
      .forEach((l) => {
        clearLane(l.id);
        regenerate(l.id, latestTurn.id);
      });
  }

  function exportSession(format: "md" | "json") {
    if (!activeId) return;
    const token = getToken();
    fetch(`${API_BASE}/api/sessions/${activeId}/export?format=${format}`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((r) => r.blob())
      .then((blob) => {
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `${session?.title || "session"}.${format}`;
        a.click();
        URL.revokeObjectURL(url);
      });
  }

  function exportComparison(fmt: "md" | "docx" | "pdf") {
    if (!activeId) return;
    apiFetch<{ url: string; download_name: string }>(
      `/api/sessions/${activeId}/export?fmt=${fmt}`,
      { method: "POST" }
    )
      .then((res) => {
        const a = document.createElement("a");
        a.href = mediaUrl(res.url);
        a.download = res.download_name;
        a.click();
        setShowFiles(true);
      })
      .catch((e) => alert((e as Error).message));
  }

  async function continueInLane(laneId: string) {
    if (!activeId) return;
    try {
      const res = await apiFetch<{ id: string }>(
        `/api/sessions/${activeId}/lanes/${laneId}/continue`,
        { method: "POST" }
      );
      qc.invalidateQueries({ queryKey: ["sessions"] });
      setActiveId(res.id);
    } catch (e) {
      alert((e as Error).message);
    }
  }

  async function branchFrom(turnId: string) {
    if (!activeId) return;
    try {
      const res = await apiFetch<{ id: string }>(
        `/api/sessions/${activeId}/branch?turn_id=${turnId}`,
        { method: "POST" }
      );
      qc.invalidateQueries({ queryKey: ["sessions"] });
      setActiveId(res.id);
    } catch (e) {
      alert((e as Error).message);
    }
  }

  async function pinAnswer(laneId: string, turnId: string) {
    if (!session) return;
    const lane = session.lanes.find((l) => l.id === laneId);
    const msg = session.messages.find(
      (m) => m.lane_id === laneId && m.turn_id === turnId && m.role === "assistant"
    );
    const turn = session.turns.find((t) => t.id === turnId);
    if (!lane || !msg || !msg.content) return;
    const provider = providers.find((p) => p.id === lane.provider_id);
    try {
      await apiFetch("/api/snapshots", {
        method: "POST",
        body: JSON.stringify({
          session_id: activeId,
          prompt: turn?.content || "",
          model: lane.model,
          provider_name: provider?.name || null,
          content: msg.content,
        }),
      });
      setShowSnapshots(true);
    } catch (e) {
      alert((e as Error).message);
    }
  }

  async function importSession(file: File) {
    const text = await file.text();
    const body = JSON.parse(text);
    const res = await fetch(`${API_BASE}/api/sessions/import`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${getToken()}`,
      },
      body: JSON.stringify(body),
    });
    const created = await res.json();
    qc.invalidateQueries({ queryKey: ["sessions"] });
    setActiveId(created.id);
  }

  function exportEverything() {
    // Full-system backup moved to Settings → General; keep a palette shortcut that jumps there.
    nav("/settings/general");
  }

  return (
    <div className="flex h-full bg-white dark:bg-gray-950">
      {navCollapsed ? (
        <div className="flex h-full w-11 shrink-0 flex-col items-center gap-1 border-r border-gray-200 bg-gray-50 py-2 dark:border-gray-700 dark:bg-gray-950">
          <button
            onClick={() => setNavCollapsed(false)}
            title="Expand sidebar"
            className="rounded p-1.5 text-gray-500 hover:bg-gray-200 dark:hover:bg-gray-800"
          >
            ⏵
          </button>
          <button
            onClick={() => newTopic()}
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
      ) : (
        <SessionSidebar
          sessions={sessions}
          personas={personas}
          activeId={activeId}
          generatingIds={generatingIds}
          onSelect={setActiveId}
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
                setActiveId(next.id);
              } else {
                localStorage.removeItem("multichat_active");
                setActiveId(null);
              }
            }
            sm.remove.mutate(id);
          }}
        />
      )}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex flex-wrap items-center gap-2 border-b border-gray-200 bg-white px-3 py-2 dark:border-gray-700 dark:bg-gray-900">
          <input
            value={session?.title || ""}
            disabled={!session}
            onChange={(e) =>
              activeId && sm.update.mutate({ id: activeId, body: { title: e.target.value } })
            }
            placeholder="Topic title"
            className="rounded border border-gray-300 px-2 py-1 text-sm font-medium dark:border-gray-600 dark:bg-gray-800"
          />
          <button
            disabled={!session}
            onClick={() => setPromptOpen((o) => !o)}
            title="Edit the shared system prompt"
            className={`rounded border px-2 py-1 text-xs disabled:opacity-40 ${
              session?.system_prompt
                ? "border-brand bg-brand/10 text-brand"
                : "border-gray-300 dark:border-gray-600"
            }`}
          >
            📝 Prompt{promptOpen ? " ▾" : ""}
          </button>
          <div className="relative">
            <button
              disabled={!session}
              onClick={() => setToolsOpen((o) => !o)}
              className={`rounded border px-2 py-1 text-xs disabled:opacity-40 ${
                session?.tools_enabled
                  ? "border-brand bg-brand/10 text-brand"
                  : "border-gray-300 dark:border-gray-600"
              }`}
            >
              🛠 Tools{session?.tools_enabled ? " ✓" : ""}
            </button>
            {toolsOpen && session && (
              <div className="absolute left-0 z-30 mt-1 w-52 rounded-lg border border-gray-200 bg-white p-2 text-xs shadow-lg dark:border-gray-700 dark:bg-gray-900">
                <label className="mb-1 flex items-center gap-2 font-medium">
                  <input
                    type="checkbox"
                    checked={session.tools_enabled || false}
                    onChange={(e) =>
                      activeId &&
                      sm.update.mutate({
                        id: activeId,
                        body: { tools_enabled: e.target.checked },
                      })
                    }
                  />
                  Enable tool use
                </label>
                <div className="mt-1 border-t border-gray-100 pt-1 dark:border-gray-800">
                  {ALL_TOOLS.map((t) => {
                    const enabled: string[] =
                      session.tool_config_json?.enabled ?? ALL_TOOLS;
                    const on = enabled.includes(t);
                    return (
                      <label
                        key={t}
                        className={`flex items-center gap-2 py-0.5 ${
                          session.tools_enabled ? "" : "opacity-50"
                        }`}
                      >
                        <input
                          type="checkbox"
                          disabled={!session.tools_enabled}
                          checked={on}
                          onChange={(e) => {
                            const next = e.target.checked
                              ? [...new Set([...enabled, t])]
                              : enabled.filter((x) => x !== t);
                            activeId &&
                              sm.update.mutate({
                                id: activeId,
                                body: {
                                  tool_config: {
                                    ...(session.tool_config_json || {}),
                                    enabled: next,
                                  },
                                },
                              });
                          }}
                        />
                        {t}
                      </label>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
          <label className="flex items-center gap-1 text-xs">
            <input
              type="checkbox"
              checked={!!judgeLane}
              disabled={!session}
              onChange={(e) => {
                if (!activeId) return;
                if (e.target.checked) {
                  // Default the judge to a known-good provider/model: reuse the first
                  // responder lane if present, else the first provider that has models.
                  const responder = session?.lanes.find(
                    (l) => l.role === "responder",
                  );
                  const usable =
                    providers.find((p) => p.default_model || p.models.length > 0) ||
                    providers[0];
                  const provider_id = responder?.provider_id || usable?.id;
                  const model =
                    responder?.model ||
                    usable?.default_model ||
                    usable?.models[0] ||
                    "";
                  if (provider_id && model)
                    sm.addLane.mutate({
                      id: activeId,
                      body: { provider_id, model, role: "judge" },
                    });
                } else if (judgeLane) {
                  sm.removeLane.mutate({ id: activeId, laneId: judgeLane.id });
                }
              }}
            />
            Judge
          </label>
          <button
            onClick={() => setShowDiff((d) => !d)}
            className="rounded border border-gray-300 px-2 py-1 text-xs dark:border-gray-600"
          >
            {showDiff ? "Grid" : "Diff"}
          </button>
          <button
            onClick={regenerateAll}
            disabled={
              !session ||
              !latestTurn ||
              streaming ||
              session.lanes.filter((l) => l.role === "responder").length === 0
            }
            title="Regenerate the latest response in every lane"
            className="rounded border border-gray-300 px-2 py-1 text-xs disabled:opacity-40 dark:border-gray-600"
          >
            ↻ Regenerate all
          </button>
          <button
            onClick={() => setShowArtifacts((a) => !a)}
            className="rounded border border-gray-300 px-2 py-1 text-xs dark:border-gray-600"
            title="Artifacts panel"
          >
            📌 Artifacts
          </button>
          <button
            onClick={() => setShowInsights((v) => !v)}
            disabled={!session}
            className={`rounded border px-2 py-1 text-xs disabled:opacity-40 ${
              showInsights
                ? "border-brand bg-brand/10 text-brand"
                : "border-gray-300 dark:border-gray-600"
            }`}
            title="Cross-lane insights (fastest, longest, synthesize)"
          >
            📊 Insights
          </button>
          <button
            onClick={() => setShowFiles((v) => !v)}
            disabled={!session}
            className={`rounded border px-2 py-1 text-xs disabled:opacity-40 ${
              showFiles
                ? "border-brand bg-brand/10 text-brand"
                : "border-gray-300 dark:border-gray-600"
            }`}
            title="Generated files for this session"
          >
            📁 Files
          </button>
          <button
            onClick={() => setShowSnapshots((v) => !v)}
            className={`rounded border px-2 py-1 text-xs ${
              showSnapshots
                ? "border-brand bg-brand/10 text-brand"
                : "border-gray-300 dark:border-gray-600"
            }`}
            title="Pinned answers — compare a model's answers across runs"
          >
            📌 Pins
          </button>
          <button
            onClick={() => setFitToScreen((v) => !v)}
            className={`rounded border px-2 py-1 text-xs ${
              fitToScreen
                ? "border-brand bg-brand/10 text-brand"
                : "border-gray-300 dark:border-gray-600"
            }`}
            title="Fit all lanes to the screen width (no horizontal scroll)"
          >
            ⤢ Fit
          </button>
          <button
            onClick={() =>
              setDensity((d) => (d === "compact" ? "comfortable" : "compact"))
            }
            className="rounded border border-gray-300 px-2 py-1 text-xs dark:border-gray-600"
            title="Toggle compact / comfortable spacing"
          >
            {density === "compact" ? "▤ Compact" : "▥ Comfortable"}
          </button>
          {Object.keys(laneWidths).length > 0 && !showDiff && (
            <button
              onClick={resetLayout}
              className="rounded border border-gray-300 px-2 py-1 text-xs dark:border-gray-600"
              title="Reset lane widths to a balanced layout"
            >
              ⇋ Balance
            </button>
          )}
          <div className="relative" ref={moreRef}>
            <button
              onClick={() => setMoreOpen((o) => !o)}
              disabled={!session}
              className="rounded border border-gray-300 px-2 py-1 text-xs disabled:opacity-40 dark:border-gray-600"
              title="More actions"
            >
              ⋯ More
            </button>
            {moreOpen && session && (
              <div
                className="absolute right-0 z-30 mt-1 w-44 rounded-lg border border-gray-200 bg-white py-1 text-xs shadow-lg dark:border-gray-700 dark:bg-gray-900"
              >
                <button
                  onClick={() => {
                    saveAsPersona();
                    setMoreOpen(false);
                  }}
                  disabled={session.lanes.length === 0}
                  className="block w-full px-3 py-1.5 text-left hover:bg-gray-100 disabled:opacity-40 dark:hover:bg-gray-800"
                >
                  ★ Save as persona
                </button>
                <button
                  onClick={() => {
                    exportSession("md");
                    setMoreOpen(false);
                  }}
                  className="block w-full px-3 py-1.5 text-left hover:bg-gray-100 dark:hover:bg-gray-800"
                >
                  ⬇ Export Markdown
                </button>
                <button
                  onClick={() => {
                    exportComparison("docx");
                    setMoreOpen(false);
                  }}
                  className="block w-full px-3 py-1.5 text-left hover:bg-gray-100 dark:hover:bg-gray-800"
                >
                  ⬇ Export as Word (.docx)
                </button>
                <button
                  onClick={() => {
                    exportComparison("pdf");
                    setMoreOpen(false);
                  }}
                  className="block w-full px-3 py-1.5 text-left hover:bg-gray-100 dark:hover:bg-gray-800"
                >
                  ⬇ Export as PDF
                </button>
                <button
                  onClick={() => {
                    exportSession("json");
                    setMoreOpen(false);
                  }}
                  className="block w-full px-3 py-1.5 text-left hover:bg-gray-100 dark:hover:bg-gray-800"
                >
                  ⬇ Export JSON
                </button>
                <label className="block w-full cursor-pointer px-3 py-1.5 text-left hover:bg-gray-100 dark:hover:bg-gray-800">
                  ⬆ Import JSON
                  <input
                    type="file"
                    accept="application/json"
                    hidden
                    onChange={(e) => {
                      if (e.target.files?.[0]) importSession(e.target.files[0]);
                      setMoreOpen(false);
                    }}
                  />
                </label>
              </div>
            )}
          </div>
          <div className="ml-auto flex items-center gap-2">
          <Link
            to="/settings"
            className="rounded border border-gray-300 px-2 py-1 text-xs dark:border-gray-600"
          >
            Settings
          </Link>
          <ThemeToggle />
          <span className="text-xs text-gray-500">{user?.email}</span>
          <button
            onClick={logout}
            className="rounded border border-gray-300 px-2 py-1 text-xs dark:border-gray-600"
          >
            Sign out
          </button>
          </div>
        </header>

        {promptOpen && session && (
          <div className="border-b border-gray-200 bg-gray-50 px-3 py-2 dark:border-gray-700 dark:bg-gray-900/40">
            <div className="mb-1 flex items-center justify-between">
              <label className="text-xs font-medium text-gray-500">
                Shared system prompt
              </label>
              <span className="text-[10px] text-gray-400">
                ~{Math.ceil((session.system_prompt || "").length / 4)} tokens ·{" "}
                {(session.system_prompt || "").length} chars
              </span>
            </div>
            <textarea
              value={session.system_prompt || ""}
              onChange={(e) =>
                activeId &&
                sm.update.mutate({
                  id: activeId,
                  body: { system_prompt: e.target.value },
                })
              }
              placeholder="Instructions applied to every lane in this topic…"
              rows={4}
              className="w-full resize-y rounded border border-gray-300 px-2 py-1 text-sm dark:border-gray-600 dark:bg-gray-800"
            />
          </div>
        )}

        {showFiles && session && (
          <FilesPanel sessionId={activeId!} onClose={() => setShowFiles(false)} />
        )}

        {showSnapshots && (
          <SnapshotsPanel onClose={() => setShowSnapshots(false)} />
        )}

        {showInsights && session && !showDiff && (
          <InsightsPanel
            sessionId={activeId!}
            lanes={session.lanes}
            providers={providers}
            messages={session.messages}
            live={live}
            latestTurn={latestTurn}
            onClose={() => setShowInsights(false)}
          />
        )}

        {providers.length > 0 && session && (
          <div className="border-b border-gray-100 px-3 py-2 dark:border-gray-800">
            <ModelPicker providers={providers} onAdd={addLane} allowJudge />
          </div>
        )}
        {providers.length === 0 && (
          <div className="border-b border-gray-100 px-3 py-2 text-sm text-amber-600 dark:border-gray-800">
            No providers configured.{" "}
            <Link to="/settings" className="underline">
              Add one in Settings
            </Link>
            .
          </div>
        )}

        <div className="min-h-0 flex-1">
          {!session ? (
            <div className="flex h-full items-center justify-center text-sm text-gray-500">
              Select or create a topic to begin.
            </div>
          ) : showDiff ? (
            <DiffView
              lanes={session.lanes}
              providers={providers}
              messages={session.messages}
              turn={latestTurn}
            />
          ) : (
            <CompareGrid
              lanes={session.lanes}
              providers={providers}
              messages={session.messages}
              turns={session.turns}
              live={mergedLive}
              streaming={streaming}
              bestLaneId={activeId ? bestLane[activeId] || null : null}
              onStop={stopLane}
              onRegenerate={(laneId) => {
                clearLane(laneId);
                regenerate(laneId, latestTurn?.id);
              }}
              onRemove={(laneId) =>
                activeId && sm.removeLane.mutate({ id: activeId, laneId })
              }
              onPickBest={setBest}
              onContinue={continueInLane}
              onBranchTurn={branchFrom}
              onPinAnswer={pinAnswer}
              onEditTurn={(turnId, content) => {
                if (!activeId) return;
                sm.deleteTurn.mutate({ id: activeId, turnId });
                setEditDraft({ text: content, ts: Date.now() });
              }}
              onResendTurn={async (content) => {
                if (!activeId) return;
                // Re-broadcast this prompt as a new turn (interrupting any in-flight
                // response, like the composer's Send does).
                if (streaming) await stopAll();
                else clearLive();
                broadcast(content);
              }}
              onResendTurnHere={async (content, laneId) => {
                if (!activeId) return;
                // Re-send this prompt to THIS lane only (new turn targeting just this lane).
                // Interrupt only this lane if it's mid-response; leave the others alone.
                if (
                  live[laneId]?.status === "streaming" ||
                  live[laneId]?.status === "queued"
                ) {
                  await stopLane(laneId);
                }
                clearLane(laneId);
                broadcast(content, undefined, [laneId], `resendhere:${laneId}:${Date.now()}`);
              }}
              onSendToLane={async (content, laneId) => {
                if (!activeId) return;
                // Per-lane composer: send a brand-new message to THIS lane only. Interrupt
                // only this lane if it's mid-response; leave the others alone.
                if (
                  live[laneId]?.status === "streaming" ||
                  live[laneId]?.status === "queued"
                ) {
                  await stopLane(laneId);
                }
                clearLane(laneId);
                broadcast(content, undefined, [laneId], `lanemsg:${laneId}:${Date.now()}`);
              }}
              onDeleteTurn={(turnId) => {
                if (!activeId) return;
                if (confirm("Delete this turn (prompt + all lane replies)?"))
                  sm.deleteTurn.mutate({ id: activeId, turnId });
              }}
              onRegenerateTurn={(laneId, turnId) => {
                clearLane(laneId);
                regenerate(laneId, turnId);
              }}
              onUpdateLane={(laneId, body) => {
                if (!activeId) return;
                sm.updateLane.mutate({ id: activeId, laneId, body });
              }}
              onCloseLane={(laneId) => {
                if (!activeId) return;
                sm.updateLane.mutate({ id: activeId, laneId, body: { hidden: true } });
              }}
              onReopenLane={(laneId) => {
                if (!activeId) return;
                sm.updateLane.mutate({ id: activeId, laneId, body: { hidden: false } });
              }}
              laneWidths={laneWidths}
              onResizeLane={setLaneWidth}
              density={density}
              fitToScreen={fitToScreen}
              queuedByLane={queuedByLane}
              onSendQueuedNow={sendQueuedNowToLane}
              onRemoveQueued={removeQueued}
            />
          )}
        </div>

        {session && judgeLane && (
          <JudgePanel
            judgeLane={judgeLane}
            latestTurn={latestTurn}
            messages={session.messages}
            live={live[judgeLane.id]}
            streaming={streaming}
            onRun={judge}
          />
        )}

        {session && (
          <LaneComposer
            lanes={session.lanes}
            disabled={session.lanes.filter((l) => l.role === "responder").length === 0}
            streaming={anyLaneBusy}
            queue={queue}
            onEnqueue={(msg) => setQueue((q) => [...q, msg])}
            onRemoveQueued={(id) => setQueue((q) => q.filter((m) => m.id !== id))}
            initialText={editDraft?.text}
            initialTextKey={editDraft?.ts}
            autoFocusKey={activeId ?? undefined}
            onSend={async (content, attachmentIds, targetLaneIds) => {
              // Sending while a response is still streaming interrupts it: stop the
              // in-flight lanes (keeping the partial answer generated so far) and
              // then start the new request as fresh response messages.
              if (streaming) await stopAll();
              else clearLive();
              broadcast(content, attachmentIds, targetLaneIds);
            }}
          />
        )}
      </div>

      {showArtifacts && <ArtifactPanel onClose={() => setShowArtifacts(false)} />}

      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        sessions={sessions}
        personas={personas}
        onSelectSession={setActiveId}
        onNew={newTopic}
        onOpenSettings={() => nav("/settings")}
        onToggleTheme={theme.toggle}
        onToggleDiff={() => setShowDiff((d) => !d)}
        extraCommands={
          session
            ? [
                { id: "cmd-files", label: "Toggle Files panel", hint: "panel", run: () => setShowFiles((v) => !v) },
                { id: "cmd-snapshots", label: "Toggle Pinned answers (compare runs)", hint: "panel", run: () => setShowSnapshots((v) => !v) },
                { id: "cmd-insights", label: "Toggle Insights", hint: "panel", run: () => setShowInsights((v) => !v) },
                { id: "cmd-prompt", label: "Toggle Prompt drawer", hint: "panel", run: () => setPromptOpen((v) => !v) },
                { id: "cmd-fit", label: "Toggle Fit-to-screen", hint: "layout", run: () => setFitToScreen((v) => !v) },
                { id: "cmd-export-pdf", label: "Export comparison as PDF", hint: "export", run: () => exportComparison("pdf") },
                { id: "cmd-export-docx", label: "Export comparison as Word", hint: "export", run: () => exportComparison("docx") },
                { id: "cmd-export-md", label: "Export comparison as Markdown", hint: "export", run: () => exportComparison("md") },
                { id: "cmd-backup", label: "Full system backup (Settings)", hint: "backup", run: () => exportEverything() },
                {
                  id: "cmd-vision",
                  label: "Extract data from an image → table",
                  hint: "vision",
                  run: () =>
                    setEditDraft({
                      text:
                        "Attached is an image (a chart, screenshot, or table). Extract all the data from it into a clean Markdown table, and note any titles, axes, or units.",
                      ts: Date.now(),
                    }),
                },
                ...(latestTurn
                  ? [{ id: "cmd-branch", label: "Branch from the latest turn", hint: "fork", run: () => branchFrom(latestTurn.id) }]
                  : []),
              ]
            : []
        }
      />
    </div>
  );
}
