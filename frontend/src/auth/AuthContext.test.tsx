import { act, StrictMode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { isCancelledError, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch, apiFetchCached, clearEtagCache, getToken, setToken } from "../api/client";
import type { User } from "../api/types";
import { AuthProvider, useAuth } from "./AuthContext";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

// Like the existing hook tests: real React DOM with no host nodes, no new DOM dependency.
function emptyContainer(): HTMLElement {
  const document = {
    nodeType: 9, activeElement: null, body: null,
    addEventListener() {}, removeEventListener() {}, defaultView: {},
  };
  document.defaultView = { document, HTMLIFrameElement: class {} };
  return {
    nodeType: 1, tagName: "DIV", nodeName: "DIV",
    namespaceURI: "http://www.w3.org/1999/xhtml", ownerDocument: document,
    textContent: "", addEventListener() {}, removeEventListener() {},
  } as unknown as HTMLElement;
}

const alice: User = {
  id: "alice", email: "alice@example.test", created_at: "2026-09-04T00:00:00Z",
  is_default_password: true,
};
const bob: User = { ...alice, id: "bob", email: "bob@example.test" };
const fetchMock = vi.fn<typeof fetch>();
let root: Root | null;
let queryClient: QueryClient;
let current: ReturnType<typeof useAuth>;

function jsonResponse(body: unknown, etag?: string) {
  return new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json", ...(etag ? { ETag: etag } : {}) },
  });
}

function Probe() {
  current = useAuth();
  return null;
}

async function render(strict = false) {
  await act(async () => {
    const provider = <QueryClientProvider client={queryClient}>
      <AuthProvider><Probe /></AuthProvider>
    </QueryClientProvider>;
    root!.render(strict ? <StrictMode>{provider}</StrictMode> : provider);
  });
}

async function signedIn() {
  setToken("alice-token");
  fetchMock.mockResolvedValueOnce(jsonResponse({ user: alice }));
  await render();
  expect(current.user).toEqual(alice);
}

async function signInBob() {
  fetchMock.mockResolvedValueOnce(jsonResponse({ token: "bob-token", user: bob }));
  await act(async () => { await current.login(bob.email, "test-password"); });
}

beforeEach(() => {
  const storage = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
    removeItem: (key: string) => storage.delete(key),
  });
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("window", { event: undefined });
  // No request can reach a live endpoint, including an unexpected request.
  vi.stubGlobal("fetch", fetchMock.mockReset().mockImplementation(() => {
    throw new Error("Unexpected network request");
  }));
  clearEtagCache();
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  root = createRoot(emptyContainer());
});

afterEach(async () => {
  if (root) await act(async () => { root!.unmount(); });
  root = null;
  queryClient.clear();
  setToken(null);
  vi.unstubAllGlobals();
});

describe.each(["logout", "sign-in", "logout then sign-in"])("query isolation on %s", (boundary) => {
  it.each(["200", "304", "plain"])("cancels an old %s global-key query before a new identity fetches", async (kind) => {
    await signedIn();
    const path = "/api/sessions/shared";
    const key = kind === "plain" ? ["providers"] : ["session", "shared"];
    const read = () => kind === "plain" ? apiFetch(path) : apiFetchCached(path);
    if (kind === "304") {
      fetchMock.mockResolvedValueOnce(jsonResponse({ owner: "alice" }, '"alice"'));
      await apiFetchCached(path);
    }
    queryClient.setQueryData(["sessions"], ["private Alice session"]);
    const pending = deferred<Response>();
    fetchMock.mockReturnValueOnce(pending.promise);
    let rawResult!: Promise<unknown>;
    const oldQuery = queryClient.fetchQuery({ queryKey: key, queryFn: () => {
      const request = read();
      rawResult = request.catch((error: unknown) => error);
      return request;
    } }).catch((error: unknown) => error);

    if (boundary !== "sign-in") await act(async () => { current.logout(); });
    if (boundary !== "logout") await signInBob();
    // Inspect synchronously before resolving Alice's transport (it ignores abort).
    const clearedKeys = queryClient.getQueryCache().getAll().map((query) => query.queryKey);
    const owner = boundary === "logout" ? "anonymous" : "bob";
    fetchMock.mockResolvedValueOnce(jsonResponse({ owner }, '"current"'));
    // Start the new same-key request before Alice settles to reproduce deduplication pollution.
    const newQuery = queryClient.fetchQuery({ queryKey: key, queryFn: read });
    pending.resolve(kind === "304" ? new Response(null, { status: 304 })
      : jsonResponse({ owner: "alice" }, '"old"'));
    const [old, raw, fresh] = await Promise.all([oldQuery, rawResult, newQuery]);
    expect(clearedKeys).toEqual([]);
    expect(isCancelledError(old)).toBe(true);
    expect(raw).toMatchObject({ name: "AbortError" });
    expect(fresh).toEqual({ owner });
    expect(queryClient.getQueryData(key)).toEqual({ owner });
    expect(queryClient.getQueryData(["sessions"])).toBeUndefined();
    expect(new Headers(fetchMock.mock.calls.at(-1)?.[1]?.headers).get("If-None-Match")).toBeNull();
    expect(current.user).toEqual(boundary === "logout" ? null : bob);
  });
});

describe("auth completion races", () => {
  it.each(["success", "401"])("ignores a startup %s while a newer login is still pending", async (outcome) => {
    setToken("alice-token");
    const me = deferred<Response>();
    fetchMock.mockReturnValueOnce(me.promise);
    await render();
    const pendingLogin = deferred<Response>();
    fetchMock.mockReturnValueOnce(pendingLogin.promise);
    const login = current.login(bob.email, "test-password");
    await act(async () => {
      me.resolve(outcome === "success" ? jsonResponse({ user: alice })
        : new Response(null, { status: 401 }));
    });
    const duringLogin = { user: current.user, loading: current.loading, token: getToken() };
    await act(async () => {
      pendingLogin.resolve(jsonResponse({ token: "bob-token", user: bob }));
      await login;
    });
    expect(duringLogin).toEqual({ user: null, loading: true, token: "alice-token" });
    expect(current.user).toEqual(bob);
    expect(current.loading).toBe(false);
  });

  it.each(["startup", "refresh"])("does not let a late %s me response sign back in after logout", async (source) => {
    if (source === "refresh") await signedIn();
    else setToken("alice-token");
    const pending = deferred<Response>();
    fetchMock.mockReturnValueOnce(pending.promise);
    let refresh: Promise<void> | undefined;
    if (source === "startup") await render();
    else await act(async () => { refresh = current.refreshUser(); });
    localStorage.setItem("multichat_active", "alice-session");
    localStorage.setItem("multichat_last", "alice-session");
    await act(async () => { current.logout(); });
    await act(async () => { pending.resolve(jsonResponse({ user: alice })); await refresh; });
    expect(current.user).toBeNull();
    expect(current.loading).toBe(false);
    expect(getToken()).toBeNull();
    expect(localStorage.getItem("multichat_active")).toBeNull();
    expect(localStorage.getItem("multichat_last")).toBeNull();
  });

  it.each(["startup", "refresh"])("ignores an old %s 401 after Bob signs in", async (source) => {
    if (source === "refresh") await signedIn();
    else setToken("alice-token");
    const pending = deferred<Response>();
    fetchMock.mockReturnValueOnce(pending.promise);
    let refresh: Promise<void> | undefined;
    if (source === "startup") await render();
    else await act(async () => { refresh = current.refreshUser(); });
    await signInBob();
    queryClient.setQueryData(["sessions"], ["Bob"]);
    await act(async () => { pending.resolve(new Response(null, { status: 401 })); await refresh; });
    expect(current.user).toEqual(bob);
    expect(getToken()).toBe("bob-token");
    expect(queryClient.getQueryData(["sessions"])).toEqual(["Bob"]);
    expect(current.loading).toBe(false);
  });

  it("rejects a login completing after logout even if the token was already null", async () => {
    await render();
    const pending = deferred<Response>();
    fetchMock.mockReturnValueOnce(pending.promise);
    const login = current.login(alice.email, "test-password").catch((error: unknown) => error);
    await act(async () => { current.logout(); });
    let result: unknown;
    await act(async () => {
      pending.resolve(jsonResponse({ token: "alice-token", user: alice }));
      result = await login;
    });
    expect(result).toMatchObject({ name: "AbortError" });
    expect(current.user).toBeNull();
    expect(getToken()).toBeNull();
  });

  it("rejects an older concurrent login while the latest attempt succeeds", async () => {
    await render();
    const first = deferred<Response>();
    const second = deferred<Response>();
    fetchMock.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const aliceLogin = current.login(alice.email, "test-password").catch((error: unknown) => error);
    const bobLogin = current.login(bob.email, "test-password");
    let result: unknown;
    await act(async () => {
      first.resolve(jsonResponse({ token: "alice-token", user: alice }));
      result = await aliceLogin;
      second.resolve(jsonResponse({ token: "bob-token", user: bob }));
      await bobLogin;
    });
    expect(result).toMatchObject({ name: "AbortError" });
    expect(current.user).toEqual(bob);
    expect(getToken()).toBe("bob-token");
  });

  it("does not allow an obsolete StrictMode startup 401 to expire the current session", async () => {
    setToken("alice-token");
    const obsolete = deferred<Response>();
    fetchMock.mockReturnValueOnce(obsolete.promise)
      .mockResolvedValueOnce(jsonResponse({ user: alice }));
    await render(true);
    queryClient.setQueryData(["sessions"], ["Alice"]);
    await act(async () => { obsolete.resolve(new Response(null, { status: 401 })); });
    expect(current.user).toEqual(alice);
    expect(getToken()).toBe("alice-token");
    expect(queryClient.getQueryData(["sessions"])).toEqual(["Alice"]);
  });

  it("does not persist a login response after the provider unmounts", async () => {
    await render();
    const pending = deferred<Response>();
    fetchMock.mockReturnValueOnce(pending.promise);
    const login = current.login(alice.email, "test-password").catch((error: unknown) => error);
    await act(async () => { root!.unmount(); root = null; });
    pending.resolve(jsonResponse({ token: "alice-token", user: alice }));
    expect(await login).toMatchObject({ name: "AbortError" });
    expect(getToken()).toBeNull();
  });
});

describe.each(["startup", "refresh"])("%s authentication failures", (source) => {
  it("expires the session, clears cached data and cancels pending queries only on 401", async () => {
    if (source === "refresh") await signedIn();
    else setToken("alice-token");
    const pending = deferred<Response>();
    fetchMock.mockReturnValueOnce(pending.promise);
    const request = queryClient.fetchQuery({ queryKey: ["providers"], queryFn: () => apiFetch("/api/providers") })
      .catch((error: unknown) => error);
    queryClient.setQueryData(["sessions"], ["Alice"]);
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 401 }));
    if (source === "startup") await render();
    else await act(async () => { await current.refreshUser(); });
    const cleared = queryClient.getQueryCache().getAll().length;
    pending.resolve(jsonResponse({ owner: "alice" }));
    const result = await request;
    expect(cleared).toBe(0);
    expect(isCancelledError(result)).toBe(true);
    expect(current.user).toBeNull();
    expect(getToken()).toBeNull();
  });

  it.each(["offline", "503"])("preserves the stored login and cache on %s", async (failure) => {
    if (source === "refresh") await signedIn();
    else setToken("alice-token");
    queryClient.setQueryData(["sessions"], ["Alice"]);
    if (failure === "offline") fetchMock.mockRejectedValueOnce(new TypeError("offline"));
    else fetchMock.mockResolvedValueOnce(new Response(null, { status: 503 }));
    if (source === "startup") await render();
    else await act(async () => { await current.refreshUser(); });
    expect(getToken()).toBe("alice-token");
    expect(queryClient.getQueryData(["sessions"])).toEqual(["Alice"]);
    expect(current.user).toEqual(source === "startup" ? null : alice);
    expect(current.loading).toBe(false);
  });
});

it("does not clear the current account or caches when a new sign-in fails", async () => {
  await signedIn();
  queryClient.setQueryData(["sessions"], ["Alice"]);
  fetchMock.mockResolvedValueOnce(jsonResponse({ owner: "alice" }, '"alice"'));
  const original = await apiFetchCached("/api/sessions/shared");
  fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ detail: "Invalid credentials" }), { status: 401 }));
  let failure: unknown;
  await act(async () => {
    failure = await current.login(bob.email, "wrong-password").catch((error: unknown) => error);
  });
  expect(failure).toMatchObject({ name: "ApiError", status: 401, message: "Invalid credentials" });
  expect(current.user).toEqual(alice);
  expect(current.loading).toBe(false);
  expect(getToken()).toBe("alice-token");
  expect(queryClient.getQueryData(["sessions"])).toEqual(["Alice"]);
  fetchMock.mockResolvedValueOnce(new Response(null, { status: 304 }));
  expect(await apiFetchCached("/api/sessions/shared")).toBe(original);
});

it("a same-identity refresh keeps pending queries, ETags and cached data usable", async () => {
  await signedIn();
  const path = "/api/sessions/shared";
  fetchMock.mockResolvedValueOnce(jsonResponse({ owner: "alice" }, '"alice"'));
  const original = await apiFetchCached(path);
  const pending = deferred<Response>();
  fetchMock.mockReturnValueOnce(pending.promise);
  const request = queryClient.fetchQuery({ queryKey: ["session", "shared"], queryFn: () => apiFetchCached(path) });
  queryClient.setQueryData(["providers"], ["Alice"]);
  const updated = { ...alice, is_default_password: false };
  fetchMock.mockResolvedValueOnce(jsonResponse({ user: updated }));
  await act(async () => { await current.refreshUser(); });
  pending.resolve(new Response(null, { status: 304 }));
  expect(await request).toBe(original);
  expect(queryClient.getQueryData(["providers"])).toEqual(["Alice"]);
  expect(current.user).toEqual(updated);
  fetchMock.mockResolvedValueOnce(new Response(null, { status: 304 }));
  expect(await apiFetchCached(path)).toBe(original);
  expect(new Headers(fetchMock.mock.calls.at(-1)?.[1]?.headers).get("If-None-Match")).toBe('"alice"');
});