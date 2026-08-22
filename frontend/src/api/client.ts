export const API_BASE =
  (import.meta.env.VITE_API_BASE as string | undefined) || "http://localhost:5001";

/** Resolve a possibly-relative API media URL (e.g. an uploaded image) against the
 * backend origin so images load from the API server, not the frontend dev server. */
export function mediaUrl(url: string): string {
  if (!url) return url;
  if (/^https?:\/\//.test(url) || url.startsWith("data:")) return url;
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

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null): void {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = { ...(extra || {}) };
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  return headers;
}

export async function apiFetch<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const isForm = options.body instanceof FormData;
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: authHeaders({
      ...(isForm ? {} : { "Content-Type": "application/json" }),
      ...(options.headers as Record<string, string>),
    }),
  });
  if (res.status === 204) return undefined as T;
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) return (await res.json()) as T;
  return (await res.text()) as unknown as T;
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

export async function apiFetchCached<T>(path: string): Promise<T> {
  const cached = etagCache.get(path);
  const res = await fetch(`${API_BASE}${path}`, {
    headers: authHeaders(
      cached ? { "If-None-Match": cached.etag } : undefined,
    ),
  });
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
    etagCache.delete(path);
    throw new Error(detail);
  }
  const body = (await res.json()) as T;
  const etag = res.headers.get("etag");
  if (etag) etagCache.set(path, { etag, body });
  else etagCache.delete(path);
  return body;
}

/** Drop cached transcript bodies (called on sign-out so nothing leaks between users). */
export function clearEtagCache(): void {
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
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop() || "";
        for (const part of parts) {
          const lines = part.split("\n");
          let event = "message";
          let dataStr = "";
          for (const line of lines) {
            if (line.startsWith("event:")) event = line.slice(6).trim();
            else if (line.startsWith("data:")) dataStr += line.slice(5).trim();
          }
          if (!dataStr) continue;
          try {
            onEvent({ event, data: JSON.parse(dataStr) });
          } catch {
            /* ignore malformed */
          }
        }
      }
      onDone?.();
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        onError?.(err as Error);
      }
    }
  })();
  return controller;
}
