import { act, createElement, StrictMode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { streamSSE } from "../api/client";
import { getDeliberation, type DeliberationRun } from "../api/deliberation";
import { useDeliberation } from "./useDeliberation";

vi.mock("../api/client", () => ({ streamSSE: vi.fn() }));
vi.mock("../api/deliberation", () => ({ getDeliberation: vi.fn() }));

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

// The probe renders no host nodes. These are only the root/event/selection surfaces
// React DOM needs; hooks, effects, cleanup, rerenders and StrictMode use real React.
// Keeping this local avoids adding a DOM package or mocking React's hook dispatcher.
function emptyContainer(): HTMLElement {
  const document = {
    nodeType: 9,
    activeElement: null,
    body: null,
    addEventListener() {},
    removeEventListener() {},
    defaultView: {},
  };
  document.defaultView = { document, HTMLIFrameElement: class {} };
  return {
    nodeType: 1,
    tagName: "DIV",
    nodeName: "DIV",
    namespaceURI: "http://www.w3.org/1999/xhtml",
    ownerDocument: document,
    textContent: "",
    addEventListener() {},
    removeEventListener() {},
  } as unknown as HTMLElement;
}

function persisted(id: string, title = id): DeliberationRun {
  return {
    id, title, session_id: `session-${id}`, turn_id: `turn-${id}`, status: "running",
    running: true, prompt: id, images: [], rounds_used: 0, converged: false, config: {},
    convergence: [], vote: {}, metrics: {}, synthesis: null, minority_report: null,
    extraction: {}, synthesis_critique: {}, total_calls: 0, wall_ms: 0, error: null,
    created_at: "2026-09-04T00:00:00Z", participants: [], steps: [],
  };
}

type Stream = {
  path: string;
  event: Parameters<typeof streamSSE>[2];
  done: () => void;
  error: (error: Error) => void;
  controller: AbortController;
};

let root: Root | null;
let current: ReturnType<typeof useDeliberation>;
let streams: Stream[];
let requests: { id: string; result: ReturnType<typeof deferred<DeliberationRun>> }[];
let renders: { requestedId: string | null; runId: string | null; started: boolean }[];

function Probe({ runId }: { runId: string | null }) {
  current = useDeliberation(runId);
  renders.push({ requestedId: runId, runId: current.run?.id ?? null, started: current.live.started });
  return null;
}

async function render(runId: string | null, strict = false) {
  await act(async () => {
    const probe = createElement(Probe, { runId });
    root!.render(strict ? createElement(StrictMode, null, probe) : probe);
  });
}

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("window", { event: undefined });
  // An unexpected request must fail rather than ever reaching a live endpoint.
  vi.stubGlobal("fetch", vi.fn(() => { throw new Error("Unexpected network request"); }));
  streams = [];
  requests = [];
  renders = [];
  vi.mocked(getDeliberation).mockReset().mockImplementation((id) => {
    const result = deferred<DeliberationRun>();
    requests.push({ id, result });
    return result.promise;
  });
  vi.mocked(streamSSE).mockReset().mockImplementation((path, _body, event, done, error) => {
    const controller = new AbortController();
    streams.push({ path, event, done: done!, error: error!, controller });
    return controller;
  });
  root = createRoot(emptyContainer());
});

afterEach(async () => {
  if (root) await act(async () => { root!.unmount(); });
  root = null;
  vi.unstubAllGlobals();
});

describe("useDeliberation run identity and cancellation", () => {
  it("ignores a pending old run response after navigation", async () => {
    await render("a");
    const oldRequest = requests[0];
    await render("b");
    expect(streams[0].controller.signal.aborted).toBe(true);
    await act(async () => { requests.at(-1)!.result.resolve(persisted("b")); });
    expect(current.run?.id).toBe("b");
    await act(async () => { oldRequest.result.resolve(persisted("a")); });
    expect(current.run?.id).toBe("b");
  });

  it("clears persisted and all live state on a different run, even before its effects", async () => {
    await render("a");
    await act(async () => {
      requests[0].result.resolve(persisted("a"));
      streams[0].event({ event: "delib_start", data: {} });
      streams[0].event({ event: "round_start", data: { round: 2, phase: "review" } });
      streams[0].event({ event: "step_start", data: { step_id: "old", round: 2, phase: "review" } });
      streams[0].event({ event: "convergence", data: { round: 2 } });
      streams[0].event({ event: "round_decision", data: { round: 2, continue: false, reason: "done" } });
      streams[0].event({ event: "metrics", data: { final_agreement: 1 } });
      streams[0].event({ event: "synthesis_done", data: { answer: "old answer" } });
      streams[0].event({ event: "delib_notice", data: { detail: "old notice" } });
      streams[0].event({ event: "delib_error", data: { detail: "old error" } });
    });
    await render("b");
    expect(current.run).toBeNull();
    expect(current.live).toEqual({
      started: false, finished: false, round: -1, phase: "", steps: {}, traces: [],
      decisions: {}, metrics: null, synthesis: null,
    });
    expect(current.loading).toBe(true);
    expect(renders.filter((entry) => entry.requestedId === "b").every(
      (entry) => entry.runId === null && !entry.started,
    )).toBe(true);
  });

  it("resets everything on null and ignores late events, errors, completion and responses", async () => {
    await render("a");
    const stream = streams[0];
    await act(async () => {
      requests[0].result.resolve(persisted("a"));
      stream.event({ event: "delib_start", data: {} });
    });
    let pending!: Promise<void>;
    await act(async () => { pending = current.refresh(); });
    const oldRequest = requests.at(-1)!;
    await render(null);
    const requestsBefore = requests.length;
    await act(async () => {
      stream.event({ event: "delib_done", data: { status: "done" } });
      stream.error(new Error("late error"));
      stream.done();
      oldRequest.result.resolve(persisted("a"));
      await pending;
    });
    expect(current.run).toBeNull();
    expect(current.loading).toBe(false);
    expect(current.live).toEqual({
      started: false, finished: false, round: -1, phase: "", steps: {}, traces: [],
      decisions: {}, metrics: null, synthesis: null,
    });
    expect(requests).toHaveLength(requestsBefore);
    expect(stream.controller.signal.aborted).toBe(true);
  });

  it("suppresses callbacks from an aborted stream when reattaching the same run", async () => {
    await render("a");
    const first = streams[0];
    await act(async () => { current.attach(); });
    const second = streams.at(-1)!;
    const requestsBefore = requests.length;
    await act(async () => {
      second.event({ event: "delib_start", data: {} });
      first.event({ event: "synthesis_done", data: { answer: "stale" } });
      first.event({ event: "delib_done", data: { status: "done" } });
      first.error(new Error("old error"));
      first.done();
    });
    expect(first.controller.signal.aborted).toBe(true);
    expect(current.live.started).toBe(true);
    expect(current.live.finished).toBe(false);
    expect(current.live.synthesis).toBeNull();
    expect(current.live.error).toBeUndefined();
    expect(current.loading).toBe(true);
    expect(requests).toHaveLength(requestsBefore);
  });

  it("rejects the earlier generation when navigating a -> b -> a", async () => {
    await render("a");
    const originalRequest = requests[0];
    const originalStream = streams[0];
    await render("b");
    await render("a");
    await act(async () => { requests.at(-1)!.result.resolve(persisted("a", "new generation")); });
    await act(async () => {
      originalRequest.result.resolve(persisted("a", "old generation"));
      originalStream.event({ event: "delib_error", data: { detail: "stale" } });
    });
    expect(current.run?.title).toBe("new generation");
    expect(current.live.error).toBeUndefined();
  });

  it("does not let an older refresh replace the newest snapshot of the same run", async () => {
    await render("a");
    const original = requests[0];
    let latest!: Promise<void>;
    await act(async () => { latest = current.refresh(); });
    await act(async () => {
      requests.at(-1)!.result.resolve(persisted("a", "new snapshot"));
      await latest;
    });
    await act(async () => { original.result.resolve(persisted("a", "old snapshot")); });
    expect(current.run?.title).toBe("new snapshot");
  });

  it("refreshes canonical data on delib_done without restarting the stream", async () => {
    await render("a");
    await render("a");
    expect(streams).toHaveLength(1);
    const beforeDone = requests.length;
    await act(async () => { streams[0].event({ event: "delib_done", data: { status: "done" } }); });
    expect(current.live.finished).toBe(true);
    expect(requests.length).toBeGreaterThan(beforeDone);
    await act(async () => { requests.at(-1)!.result.resolve(persisted("a", "completed")); });
    expect(current.run?.title).toBe("completed");
    await act(async () => { streams[0].done(); });
    expect(current.loading).toBe(false);
    expect(streams).toHaveLength(1);
  });

  it("still reports active stream errors and refreshes its run", async () => {
    await render("a");
    const beforeError = requests.length;
    await act(async () => { streams[0].error(new Error("active failure")); });
    expect(current.loading).toBe(false);
    expect(current.live.error).toBe("active failure");
    expect(requests.length).toBeGreaterThan(beforeError);
  });

  it("invalidates work and retained callbacks on unmount", async () => {
    await render("a");
    const retained = current;
    const stream = streams[0];
    const beforeUnmount = requests.length;
    await act(async () => { root!.unmount(); root = null; });
    await act(async () => {
      stream.event({ event: "delib_done", data: {} });
      stream.done();
      stream.error(new Error("late failure"));
      retained.attach();
      void retained.refresh();
    });
    expect(stream.controller.signal.aborted).toBe(true);
    expect(streams).toHaveLength(1);
    expect(requests).toHaveLength(beforeUnmount);
  });

  it("handles StrictMode effect replay without accepting canceled-generation work", async () => {
    await render("a", true);
    expect(streams).toHaveLength(2);
    const first = streams[0];
    const latest = streams[1];
    expect(first.controller.signal.aborted).toBe(true);
    await act(async () => {
      requests.at(-1)!.result.resolve(persisted("a", "current"));
      latest.event({ event: "delib_start", data: {} });
    });
    await act(async () => {
      requests[0].result.resolve(persisted("a", "obsolete"));
      first.error(new Error("replayed error"));
      first.done();
    });
    expect(current.run?.title).toBe("current");
    expect(current.live.started).toBe(true);
    expect(current.live.error).toBeUndefined();
    expect(current.loading).toBe(true);
    expect(latest.controller.signal.aborted).toBe(false);
  });
});