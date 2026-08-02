/**
 * Remembers the last conversation the user had open so landing on "/" reopens it.
 * A deliberation (/d/:runId) counts just as much as a chat (/c/:sessionId) — the old
 * behaviour only tracked chats, so a panel was silently replaced by the last chat.
 */
const KEY = "multichat_last";

export function rememberLast(path: string) {
  localStorage.setItem(KEY, path);
}

export function readLast(): string | null {
  const path = localStorage.getItem(KEY);
  if (path && (path.startsWith("/c/") || path.startsWith("/d/"))) return path;
  // Legacy key: chats only, stored as a bare session id.
  const legacy = localStorage.getItem("multichat_active");
  return legacy ? `/c/${legacy}` : null;
}

/** Drop the memory when it points at something that no longer exists (e.g. deleted). */
export function forgetLast(path: string) {
  if (localStorage.getItem(KEY) === path) localStorage.removeItem(KEY);
}
