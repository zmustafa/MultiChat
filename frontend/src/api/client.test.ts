import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

let client: typeof import("./client");
const fetchMock = vi.fn<typeof fetch>();

beforeEach(async () => {
  const storage = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
    removeItem: (key: string) => storage.delete(key),
  });
  vi.stubGlobal("window", { location: new URL("https://ui.example.test/chat") });
  vi.stubGlobal("fetch", fetchMock.mockReset());
  vi.stubEnv("VITE_API_BASE", "https://api.example.test");
  vi.resetModules();
  client = await import("./client");
  client.setToken("alice-token");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("fetchMediaBlob credential boundary", () => {
  it.each([
    "/api/uploads/image.png",
    "https://api.example.test/api/files/image.png",
    "HTTPS://api.example.test/api/files/image.png",
    "//api.example.test/api/files/image.png",
  ])("authenticates API media: %s", async (url) => {
    fetchMock.mockResolvedValue(new Response("image", {
      headers: { "Content-Disposition": "attachment; filename*=UTF-8''my%20image.png" },
    }));
    const result = await client.fetchMediaBlob(url);
    const [target, options] = fetchMock.mock.calls[0];
    expect(new URL(String(target)).origin).toBe("https://api.example.test");
    expect(new Headers(options?.headers).get("Authorization")).toBe("Bearer alice-token");
    // Even a same-origin redirect could forward the header outside /api/.
    expect(options?.redirect).toBe("error");
    expect(result.filename).toBe("my image.png");
    expect(await result.blob.text()).toBe("image");
  });

  it.each([
    "https://remote.example.test/api/image.png",
    "HTTPS://remote.example.test/api/image.png",
    "//remote.example.test/api/image.png",
    "https://api.example.test.evil.test/api/image.png",
    "https://api.example.test@remote.example.test/api/image.png",
    "http://api.example.test/api/image.png",
    "https://api.example.test:444/api/image.png",
    "https://api.example.test/public/image.png",
    "/api/../public/image.png",
    "/api/%2e%2e/public/image.png",
    "/api-not-really/image.png",
    "https://ui.example.test/api/image.png",
    "data:image/png;base64,aGVsbG8=",
  ])("does not attach credentials to untrusted media: %s", async (url) => {
    fetchMock.mockResolvedValue(new Response("image"));
    await client.fetchMediaBlob(url);
    const [, options] = fetchMock.mock.calls[0];
    expect(new Headers(options?.headers).has("Authorization")).toBe(false);
    expect(options?.credentials).toBe("omit");
  });

  it("resolves protocol-relative remote URLs as remote, not as API paths", async () => {
    fetchMock.mockResolvedValue(new Response("image"));
    await client.fetchMediaBlob("//remote.example.test/api/image.png");
    expect(String(fetchMock.mock.calls[0][0])).toBe("https://remote.example.test/api/image.png");
  });

  it("supports the production same-origin API proxy", async () => {
    vi.stubEnv("VITE_API_BASE", "");
    vi.resetModules();
    client = await import("./client");
    fetchMock.mockResolvedValue(new Response("image"));
    await client.fetchMediaBlob("/api/files/image.png");
    expect(String(fetchMock.mock.calls[0][0])).toBe("https://ui.example.test/api/files/image.png");
    expect(new Headers(fetchMock.mock.calls[0][1]?.headers).get("Authorization"))
      .toBe("Bearer alice-token");
  });
});

describe("mediaUrl absolute HTTP(S) URLs", () => {
  it.each([
    "HTTP://remote.example.test/image.png",
    "Https://remote.example.test/image.png",
  ])("preserves the external URL used by AuthenticatedMedia: %s", (url) => {
    expect(client.mediaUrl(url)).toBe(url);
  });
});

describe("fetchMediaBlob download filenames", () => {
  it.each<[string, string | undefined]>([
    ["attachment; filename*=UTF-8''dir%2Fcaf%C3%A9%5Cnotes.pdf; filename=\"fallback.pdf\"", "dir_café_notes.pdf"],
    ["attachment; filename*=\"UTF-8''caf%C3%A9.pdf\"; filename=\"fallback.pdf\"", "café.pdf"],
    ["attachment; filename*=UTF-8''bad%ZZ.pdf; filename=\"reports/safe.pdf\"", "reports_safe.pdf"],
    ["attachment; filename*=UTF-8''bad%C3%28.pdf; filename=\"safe.pdf\"", "safe.pdf"],
    ["attachment; filename*=\"UTF-8''bad%ZZ.pdf\"; filename=\"safe.pdf\"", "safe.pdf"],
    ["attachment; filename*=UTF-8''bad%.pdf", undefined],
    ['attachment; filename="reports\\safe.pdf"', "reports_safe.pdf"],
    ["", undefined],
  ])("keeps the blob usable with disposition %j", async (disposition, filename) => {
    fetchMock.mockResolvedValueOnce(new Response("valid file", {
      headers: { "Content-Disposition": disposition },
    }));
    const result = await client.fetchMediaBlob("https://remote.example.test/file.pdf");
    expect(result.filename).toBe(filename);
    expect(await result.blob.text()).toBe("valid file");
    const options = fetchMock.mock.calls[0][1];
    expect(new Headers(options?.headers).has("Authorization")).toBe(false);
    expect(options?.credentials).toBe("omit");
  });

  it.each([
    ["saved.pdf", "saved.pdf"],
    [undefined, "download"],
  ])("downloads with fallback %j when the header cannot be decoded", async (fallback, expected) => {
    fetchMock.mockResolvedValueOnce(new Response("valid file", {
      headers: { "Content-Disposition": "attachment; filename*=UTF-8''bad%.pdf" },
    }));
    const link = { href: "", download: "", click: vi.fn(), remove: vi.fn() };
    const appendChild = vi.fn();
    vi.stubGlobal("document", { createElement: () => link, body: { appendChild } });
    const createObjectURL = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:download");
    const revokeObjectURL = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
    try {
      await client.downloadMedia("https://remote.example.test/file.pdf", fallback);
      expect(link.download).toBe(expected);
      expect(link.href).toBe("blob:download");
      expect(await (createObjectURL.mock.calls[0][0] as Blob).text()).toBe("valid file");
      expect(appendChild).toHaveBeenCalledWith(link);
      expect(link.click).toHaveBeenCalledOnce();
      expect(link.remove).toHaveBeenCalledOnce();
      expect(revokeObjectURL).toHaveBeenCalledWith("blob:download");
    } finally {
      createObjectURL.mockRestore();
      revokeObjectURL.mockRestore();
    }
  });
});

function jsonResponse(body: unknown, etag?: string) {
  return new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json", ...(etag ? { ETag: etag } : {}) },
  });
}

function lastRequestHeaders() {
  return new Headers(fetchMock.mock.calls.at(-1)?.[1]?.headers);
}

describe("apiFetchCached auth generations", () => {
  const path = "/api/sessions/transcript";

  it("reuses a body on 304 for the same token", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ owner: "alice" }, '"alice"'));
    const first = await client.apiFetchCached(path);
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 304 }));
    expect(await client.apiFetchCached(path)).toBe(first);
    expect(lastRequestHeaders().get("If-None-Match")).toBe('"alice"');
  });

  it("does not repopulate a cleared cache from a pending response", async () => {
    const pending = deferred<Response>();
    fetchMock.mockReturnValueOnce(pending.promise);
    const oldRequest = client.apiFetchCached(path);
    client.clearEtagCache();
    pending.resolve(jsonResponse({ owner: "alice" }, '"stale"'));
    expect(await oldRequest).toEqual({ owner: "alice" });
    fetchMock.mockResolvedValueOnce(jsonResponse({ owner: "alice" }, '"fresh"'));
    await client.apiFetchCached(path);
    expect(lastRequestHeaders().has("If-None-Match")).toBe(false);
  });

  it("guards invalidation while the response body is still pending", async () => {
    const pendingBody = deferred<unknown>();
    const response = jsonResponse({}, '"stale"');
    const json = vi.spyOn(response, "json").mockReturnValue(pendingBody.promise);
    fetchMock.mockResolvedValueOnce(response);
    const oldRequest = client.apiFetchCached(path);
    await vi.waitFor(() => expect(json).toHaveBeenCalledOnce());
    client.clearEtagCache();
    pendingBody.resolve({ owner: "alice" });
    expect(await oldRequest).toEqual({ owner: "alice" });
    fetchMock.mockResolvedValueOnce(jsonResponse({ owner: "alice" }));
    await client.apiFetchCached(path);
    expect(lastRequestHeaders().has("If-None-Match")).toBe(false);
  });

  it.each(["setToken", "storage change"])("isolates existing entries on %s", async (change) => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ owner: "alice" }, '"alice"'));
    await client.apiFetchCached(path);
    if (change === "setToken") client.setToken("bob-token");
    else localStorage.setItem("multichat_token", "bob-token");
    fetchMock.mockResolvedValueOnce(jsonResponse({ owner: "bob" }, '"bob"'));
    expect(await client.apiFetchCached(path)).toEqual({ owner: "bob" });
    expect(lastRequestHeaders().get("Authorization")).toBe("Bearer bob-token");
    expect(lastRequestHeaders().has("If-None-Match")).toBe(false);
  });

  it.each(["success", "failure", "no etag"])("an old %s cannot modify the new user's entry", async (outcome) => {
    const pending = deferred<Response>();
    fetchMock.mockReturnValueOnce(pending.promise);
    const oldRequest = client.apiFetchCached(path).catch((error: unknown) => error);
    client.setToken(null);
    client.clearEtagCache(); // Ordinary logout already calls this.
    client.setToken("bob-token");
    fetchMock.mockResolvedValueOnce(jsonResponse({ owner: "bob" }, '"bob"'));
    const bob = await client.apiFetchCached(path);
    pending.resolve(outcome === "failure"
      ? new Response("gone", { status: 404 })
      : jsonResponse({ owner: "alice" }, outcome === "success" ? '"alice"' : undefined));
    expect(await oldRequest).toMatchObject({ name: "AbortError" });
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 304 }));
    expect(await client.apiFetchCached(path)).toBe(bob);
    expect(lastRequestHeaders().get("If-None-Match")).toBe('"bob"');
  });

  it("invalidates a pending request even when the token changes away and back", async () => {
    const pending = deferred<Response>();
    fetchMock.mockReturnValueOnce(pending.promise);
    const oldRequest = client.apiFetchCached(path);
    const rejected = expect(oldRequest).rejects.toMatchObject({ name: "AbortError" });
    client.setToken("bob-token");
    client.setToken("alice-token");
    pending.resolve(jsonResponse({ owner: "alice", old: true }, '"old"'));
    await rejected;
    fetchMock.mockResolvedValueOnce(jsonResponse({ owner: "alice" }));
    await client.apiFetchCached(path);
    expect(lastRequestHeaders().has("If-None-Match")).toBe(false);
  });

  it.each([200, 304])("rejects a late %s body after logout", async (status) => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ owner: "alice" }, '"alice"'));
    await client.apiFetchCached(path);
    const pending = deferred<Response>();
    fetchMock.mockReturnValueOnce(pending.promise);
    const rejected = expect(client.apiFetchCached(path)).rejects.toMatchObject({ name: "AbortError" });
    client.setToken(null);
    pending.resolve(status === 304
      ? new Response(null, { status })
      : jsonResponse({ owner: "alice" }, '"old"'));
    await rejected;
  });

  it("allows a same-identity 304 to settle after harmless ETag invalidation", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ owner: "alice" }, '"alice"'));
    const alice = await client.apiFetchCached(path);
    const pending = deferred<Response>();
    fetchMock.mockReturnValueOnce(pending.promise);
    const request = client.apiFetchCached(path);
    client.setToken("alice-token");
    client.clearEtagCache();
    pending.resolve(new Response(null, { status: 304 }));
    expect(await request).toBe(alice);
    fetchMock.mockResolvedValueOnce(jsonResponse(alice));
    await client.apiFetchCached(path);
    expect(lastRequestHeaders().has("If-None-Match")).toBe(false);
  });
});

describe.each(["apiFetch", "apiFetchCached"] as const)("%s auth result isolation", (method) => {
  it.each(["success", "error"])("rejects an old %s while decoding its body", async (outcome) => {
    const body = deferred<unknown>();
    const response = outcome === "success"
      ? jsonResponse({}, '"old"')
      : new Response(null, { status: 401 });
    const json = vi.spyOn(response, "json").mockReturnValue(body.promise);
    fetchMock.mockResolvedValueOnce(response);
    const rejected = expect(client[method]("/api/private")).rejects.toMatchObject({ name: "AbortError" });
    await vi.waitFor(() => expect(json).toHaveBeenCalledOnce());
    client.setToken("bob-token");
    client.setToken("alice-token");
    body.resolve({ owner: "alice", detail: "expired" });
    await rejected;
  });

  it("turns a late transport rejection into an auth AbortError", async () => {
    const pending = deferred<Response>();
    fetchMock.mockReturnValueOnce(pending.promise);
    const rejected = expect(client[method]("/api/private")).rejects.toMatchObject({ name: "AbortError" });
    client.setToken(null);
    pending.reject(new TypeError("offline"));
    await rejected;
  });

  it("does not invalidate a request when the same token is set again", async () => {
    const pending = deferred<Response>();
    fetchMock.mockReturnValueOnce(pending.promise);
    const request = client[method]("/api/private");
    client.setToken("alice-token");
    pending.resolve(jsonResponse({ owner: "alice" }));
    expect(await request).toEqual({ owner: "alice" });
  });
});

describe("apiFetch auth boundaries", () => {
  it.each(["text", "invalid JSON"])("rejects stale %s decoding after headers arrived", async (kind) => {
    const body = deferred<string>();
    const response = kind === "text" ? new Response("") : jsonResponse({});
    const decode = kind === "text"
      ? vi.spyOn(response, "text").mockReturnValue(body.promise)
      : vi.spyOn(response, "json").mockReturnValue(body.promise);
    fetchMock.mockResolvedValueOnce(response);
    const rejected = expect(client.apiFetch("/api/private")).rejects.toMatchObject({ name: "AbortError" });
    await vi.waitFor(() => expect(decode).toHaveBeenCalledOnce());
    client.setToken(null);
    if (kind === "text") body.resolve("Alice's private text");
    else body.reject(new SyntaxError("Invalid JSON"));
    await rejected;
  });

  it.each(["json", "text", "204", "401"])("rejects a late %s response after a token switch", async (kind) => {
    const pending = deferred<Response>();
    fetchMock.mockReturnValueOnce(pending.promise);
    const rejected = expect(client.apiFetch("/api/private")).rejects.toMatchObject({ name: "AbortError" });
    client.setToken("bob-token");
    pending.resolve(kind === "json" ? jsonResponse({ owner: "alice" })
      : kind === "text" ? new Response("alice")
      : new Response(null, { status: Number(kind) }));
    await rejected;
  });
});

function streamResponse(chunks: Uint8Array[], close = true) {
  const cancel = vi.fn();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk);
      if (close) controller.close();
    },
    cancel,
  });
  return { body, cancel, response: new Response(body) };
}

const encode = (text: string) => new TextEncoder().encode(text);

describe("streamSSE parsing and lifecycle", () => {
  it.each(["\n", "\r\n", "\r"])("parses %j delimiters across byte boundaries", async (newline) => {
    const text = [
      ": keepalive", "", "event: update", 'data: {"text":', 'data: "café 😀"}', "",
      "data: invalid", "", 'data: {"second":true}', "", "",
    ].join(newline);
    const stream = streamResponse([...encode(text)].map((byte) => new Uint8Array([byte])));
    fetchMock.mockResolvedValueOnce(stream.response);
    const onEvent = vi.fn();
    const done = deferred<void>();
    const onError = vi.fn((error: Error) => done.reject(error));
    client.streamSSE("/api/stream", {}, onEvent, () => done.resolve(), onError);
    await done.promise;
    expect(onEvent.mock.calls.map(([event]) => event)).toEqual([
      { event: "update", data: { text: "café 😀" } },
      { event: "message", data: { second: true } },
    ]);
    expect(onError).not.toHaveBeenCalled();
    expect(stream.body.locked).toBe(false);
  });

  it("joins data lines with newlines instead of changing JSON values", async () => {
    const stream = streamResponse([encode('data: 1\ndata: 2\n\ndata: 3\n\n')]);
    fetchMock.mockResolvedValueOnce(stream.response);
    const onEvent = vi.fn();
    const done = deferred<void>();
    client.streamSSE("/api/stream", {}, onEvent, () => done.resolve(), done.reject);
    await done.promise;
    expect(onEvent.mock.calls.map(([event]) => event.data)).toEqual([3]);
  });

  it("does not dispatch an unterminated event at EOF", async () => {
    const stream = streamResponse([encode('data: {"incomplete":true}\n')]);
    fetchMock.mockResolvedValueOnce(stream.response);
    const onEvent = vi.fn();
    const done = deferred<void>();
    client.streamSSE("/api/stream", {}, onEvent, () => done.resolve(), done.reject);
    await done.promise;
    expect(onEvent).not.toHaveBeenCalled();
    expect(stream.body.locked).toBe(false);
  });

  it("reports consumer errors and cancels/releases the reader", async () => {
    const stream = streamResponse([encode("data: 1\n\ndata: 2\n\n")], false);
    fetchMock.mockResolvedValueOnce(stream.response);
    const error = new Error("consumer failed");
    const onEvent = vi.fn(() => { throw error; });
    const onDone = vi.fn();
    const onError = vi.fn();
    const controller = client.streamSSE("/api/stream", {}, onEvent, onDone, onError);
    try {
      await vi.waitFor(() => expect(onError).toHaveBeenCalledWith(error));
      await vi.waitFor(() => expect(stream.body.locked).toBe(false));
      expect(stream.cancel).toHaveBeenCalledOnce();
      expect(onEvent).toHaveBeenCalledOnce();
      expect(onDone).not.toHaveBeenCalled();
    } finally {
      controller.abort();
    }
  });

  it("stops dispatching buffered events when a callback aborts", async () => {
    const stream = streamResponse([encode("data: 1\n\ndata: 2\n\n")]);
    fetchMock.mockResolvedValueOnce(stream.response);
    const onDone = vi.fn();
    const onError = vi.fn();
    const onEvent = vi.fn(() => controller.abort());
    const controller = client.streamSSE("/api/stream", {}, onEvent, onDone, onError);
    await vi.waitFor(() => expect(onEvent).toHaveBeenCalled());
    expect(onEvent).toHaveBeenCalledOnce();
    expect(onDone).not.toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
    expect(stream.body.locked).toBe(false);
  });

  it("cancels a pending read on abort without completion or error callbacks", async () => {
    const stream = streamResponse([], false);
    const acquireReader = vi.spyOn(stream.body, "getReader");
    fetchMock.mockResolvedValueOnce(stream.response);
    const onEvent = vi.fn();
    const onDone = vi.fn();
    const onError = vi.fn();
    const controller = client.streamSSE("/api/stream", {}, onEvent, onDone, onError);
    await vi.waitFor(() => expect(acquireReader).toHaveBeenCalledOnce());
    controller.abort();
    await vi.waitFor(() => expect(stream.body.locked).toBe(false));
    expect(stream.cancel).toHaveBeenCalledOnce();
    expect(onEvent).not.toHaveBeenCalled();
    expect(onDone).not.toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
  });

  it("suppresses a response that arrives after abort and releases its body", async () => {
    const pending = deferred<Response>();
    const stream = streamResponse([encode("data: 1\n\n")], false);
    fetchMock.mockReturnValueOnce(pending.promise);
    const onEvent = vi.fn();
    const onDone = vi.fn();
    const onError = vi.fn();
    const controller = client.streamSSE("/api/stream", {}, onEvent, onDone, onError);
    controller.abort();
    pending.resolve(stream.response);
    await vi.waitFor(() => expect(stream.cancel).toHaveBeenCalledOnce());
    expect(stream.body.locked).toBe(false);
    expect(onEvent).not.toHaveBeenCalled();
    expect(onDone).not.toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
  });

  it("suppresses a non-AbortError rejection after cancellation", async () => {
    const pending = deferred<Response>();
    fetchMock.mockReturnValueOnce(pending.promise);
    const onError = vi.fn();
    const controller = client.streamSSE("/api/stream", {}, vi.fn(), vi.fn(), onError);
    controller.abort();
    pending.reject(new TypeError("late network failure"));
    await Promise.resolve();
    await Promise.resolve();
    expect(onError).not.toHaveBeenCalled();
  });

  it("releases the reader on a read failure and reports it once", async () => {
    let source!: ReadableStreamDefaultController<Uint8Array>;
    const body = new ReadableStream<Uint8Array>({ start(controller) { source = controller; } });
    const acquireReader = vi.spyOn(body, "getReader");
    fetchMock.mockResolvedValueOnce(new Response(body));
    const onError = vi.fn();
    const onDone = vi.fn();
    client.streamSSE("/api/stream", {}, vi.fn(), onDone, onError);
    await vi.waitFor(() => expect(acquireReader).toHaveBeenCalledOnce());
    const error = new Error("broken read");
    source.error(error);
    await vi.waitFor(() => expect(onError).toHaveBeenCalledWith(error));
    expect(onError).toHaveBeenCalledOnce();
    expect(body.locked).toBe(false);
    expect(onDone).not.toHaveBeenCalled();
  });
});