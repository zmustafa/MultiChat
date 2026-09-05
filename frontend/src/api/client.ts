const configuredApiBase = import.meta.env.VITE_API_BASE as string | undefined;

// Production uses the preview server's same-origin /api proxy. This keeps LAN/Tailscale
// clients on the hostname they used for the UI instead of sending them to their own localhost.
export const API_BASE =
  configuredApiBase ?? (import.meta.env.PROD ? "" : "http://localhost:5001");

/** Resolve a possibly-relative API media URL (e.g. an uploaded image) against the
 * backend origin so images load from the API server, not the frontend dev server. */
export function mediaUrl(url: string): string {
  if (!url) return url;
  if (/^https?:\/\//i.test(url) || url.startsWith("data:")) return url;
  return `${API_BASE}${url}`;
}

/** Parse a backend timestamp as UTC. The API serializes naive UTC datetimes without a
 * timezone marker; `new Date()` would otherwise interpret them as local time. */
export function asUtcDate(iso: string): Date {
  if (!iso) return new Date(NaN);
  const hasTz = /[zZ]$|[+-]\d\d:?\d\d$/.test(iso);
  return new Date(hasTz ? iso : iso + "Z");
}

const TOKEN_KEY = "multichat_token";
// Unlike ETag invalidation, a token transition makes every old result unusable.
let authGeneration = 0;

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null): void {
  const previous = getToken();
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
  if (getToken() !== previous) {
    authGeneration++;
    clearEtagCache();
  }
}

function captureAuthGuard(): () => void {
  const token = getToken();
  const generation = authGeneration;
  return () => {
    // The generation also catches a token changing away and back while awaiting.
    if (generation !== authGeneration || token !== getToken()) {
      throw new DOMException("Authentication changed during the request", "AbortError");
    }
  };
}

function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = { ...(extra || {}) };
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  return headers;
}

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "ApiError";
  }
}

export async function apiFetch<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const assertCurrentAuth = captureAuthGuard();
  try {
    const isForm = options.body instanceof FormData;
    const res = await fetch(`${API_BASE}${path}`, {
      ...options,
      headers: authHeaders({
        ...(isForm ? {} : { "Content-Type": "application/json" }),
        ...(options.headers as Record<string, string>),
      }),
    });
    assertCurrentAuth();
    if (res.status === 204) return undefined as T;
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const body = await res.json();
        detail = body.detail || detail;
      } catch {
        /* ignore */
      }
      throw new ApiError(detail, res.status);
    }
    const ct = res.headers.get("content-type") || "";
    if (ct.includes("application/json")) return (await res.json()) as T;
    return (await res.text()) as unknown as T;
  } finally {
    // Cover body decoding and errors as well as the initial fetch, before callers see them.
    assertCurrentAuth();
  }
}

/** Authenticate only media on our API origin and within /api/, never arbitrary URLs. */
export async function fetchMediaBlob(url: string): Promise<{ blob: Blob; filename?: string }> {
  const apiUrl = new URL(API_BASE || "/", window.location.href);
  const resolved = new URL(url, apiUrl);
  const isApiMedia =
    /^https?:$/.test(resolved.protocol) &&
    resolved.origin === apiUrl.origin &&
    resolved.pathname.startsWith("/api/") &&
    !resolved.username && !resolved.password;
  const res = await fetch(resolved.href, {
    headers: isApiMedia ? authHeaders() : {},
    credentials: "omit",
    // A redirect could forward Bearer auth to a same-origin non-API endpoint.
    ...(isApiMedia ? { redirect: "error" as const } : {}),
  });
  if (!res.ok) throw new ApiError(res.statusText || "Could not load file", res.status);
  const disposition = res.headers.get("content-disposition") || "";
  // Tolerate nonstandard quotes around the RFC 5987 extended value.
  const extended = /filename\*=(?:"UTF-8''([^"]*)"|UTF-8''([^;]*))/i.exec(disposition);
  const encoded = extended?.[1] ?? extended?.[2]?.trim();
  const quoted = /filename="([^"]+)"/i.exec(disposition)?.[1];
  let filename = quoted;
  if (encoded) {
    try {
      filename = decodeURIComponent(encoded);
    } catch {
      // A malformed filename must not discard valid bytes; keep filename= or the caller's fallback.
    }
  }
  if (filename) filename = filename.replace(/[\\/]/g, "_");
  return { blob: await res.blob(), filename };
}

/** Download a protected API file without putting the long-lived Bearer token in its URL. */
export async function downloadMedia(url: string, fallbackName?: string): Promise<void> {
  const { blob, filename } = await fetchMediaBlob(url);
  const objectUrl = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = filename || fallbackName || "download";
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(objectUrl);
}

export interface SSEEvent {
  event: string;
  data: any;
}

/**
 * Conditional GET. A chat transcript is refetched every time a lane finishes and is
 * almost always unchanged, so we keep the last ETag + body per path and let the server
 * answer 304 instead of re-sending hundreds of KB of markdown.
 */
const etagCache = new Map<string, { etag: string; body: unknown }>();
let etagToken: string | null = null;
let etagGeneration = 0;

export async function apiFetchCached<T>(path: string): Promise<T> {
  const token = getToken();
  // Also notice token changes made by another tab through localStorage.
  if (token !== etagToken) {
    clearEtagCache();
    etagToken = token;
  }
  const generation = etagGeneration;
  const isCurrent = () => generation === etagGeneration && token === getToken();
  const assertCurrentAuth = captureAuthGuard();
  try {
    const cached = etagCache.get(path);
    const res = await fetch(`${API_BASE}${path}`, {
      headers: authHeaders(
        cached ? { "If-None-Match": cached.etag } : undefined,
      ),
    });
    assertCurrentAuth();
    if (res.status === 304 && cached) return cached.body as T;
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const body = await res.json();
        detail = body.detail || detail;
      } catch {
        /* ignore */
      }
      // A stale entry must not outlive a failed revalidation.
      if (isCurrent()) etagCache.delete(path);
      throw new ApiError(detail, res.status);
    }
    const body = (await res.json()) as T;
    const etag = res.headers.get("etag");
    // clearEtagCache/sign-out may happen while either fetch or JSON decoding awaits.
    if (isCurrent()) {
      if (etag) etagCache.set(path, { etag, body });
      else etagCache.delete(path);
    }
    return body;
  } finally {
    // Reject stale 200/304 results too, not just their writes to the ETag cache.
    assertCurrentAuth();
  }
}

/** Drop cached transcript bodies (called on sign-out so nothing leaks between users). */
export function clearEtagCache(): void {
  etagGeneration++;
  etagCache.clear();
}

/**
 * POST a request and consume an SSE stream, invoking onEvent for each parsed event.
 * Returns an AbortController so the caller can cancel.
 */
export function streamSSE(
  path: string,
  body: unknown,
  onEvent: (evt: SSEEvent) => void,
  onDone?: () => void,
  onError?: (err: Error) => void
): AbortController {
  const controller = new AbortController();
  (async () => {
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    let finishedReading = false;
    let cancellation: Promise<void> | undefined;
    const cancelReader = () => {
      if (reader && !cancellation) {
        // Cancellation can reject if the source already errored; preserve the original error.
        cancellation = reader.cancel().catch(() => {});
      }
    };
    controller.signal.addEventListener("abort", cancelReader);
    try {
      const res = await fetch(`${API_BASE}${path}`, {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (!res.ok || !res.body) {
        throw new Error(`Stream failed: HTTP ${res.status}`);
      }
      reader = res.body.getReader();
      if (controller.signal.aborted) return;
      const decoder = new TextDecoder();
      let buffer = "";
      let event = "message";
      let dataLines: string[] = [];
      const consumeLine = (line: string) => {
        if (line === "") {
          const name = event || "message";
          const dataStr = dataLines.join("\n");
          event = "message";
          dataLines = [];
          if (!dataStr) return;
          let data: unknown;
          try {
            data = JSON.parse(dataStr);
          } catch {
            return; // Ignore malformed JSON, not exceptions thrown by the consumer.
          }
          onEvent({ event: name, data });
          return;
        }
        if (line.startsWith(":")) return;
        const colon = line.indexOf(":");
        const field = colon < 0 ? line : line.slice(0, colon);
        let value = colon < 0 ? "" : line.slice(colon + 1);
        if (value.startsWith(" ")) value = value.slice(1);
        if (field === "event") event = value;
        else if (field === "data") dataLines.push(value);
      };
      const consume = (text: string, eof = false) => {
        buffer += text;
        const endings = /\r\n|\r|\n/g;
        let start = 0;
        let match: RegExpExecArray | null;
        while (!controller.signal.aborted && (match = endings.exec(buffer))) {
          // A CR at a chunk boundary may be the first half of CRLF.
          if (!eof && match[0] === "\r" && endings.lastIndex === buffer.length) break;
          const line = buffer.slice(start, match.index);
          start = endings.lastIndex;
          consumeLine(line);
        }
        buffer = buffer.slice(start);
      };
      while (!controller.signal.aborted) {
        const { done, value } = await reader.read();
        if (controller.signal.aborted) return;
        if (done) {
          finishedReading = true;
          consume(decoder.decode(), true);
          break; // Per SSE framing, an event without a terminating blank line is incomplete.
        }
        consume(decoder.decode(value, { stream: true }));
      }
      if (!controller.signal.aborted) onDone?.();
    } catch (err) {
      if (!controller.signal.aborted && (err as Error).name !== "AbortError") {
        onError?.(err as Error);
      }
    } finally {
      controller.signal.removeEventListener("abort", cancelReader);
      if (reader) {
        if (!finishedReading) cancelReader();
        if (cancellation) await cancellation;
        reader.releaseLock();
      }
    }
  })();
  return controller;
}
