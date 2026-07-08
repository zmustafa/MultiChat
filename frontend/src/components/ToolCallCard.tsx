import { useState } from "react";
import type { LiveToolCall } from "../hooks/useBroadcast";

// Pick a short, human-friendly preview of what a tool call is doing from its
// arguments (e.g. the search query, the URL being fetched, a file path).
const PREVIEW_KEYS = [
  "query",
  "q",
  "search",
  "search_query",
  "url",
  "urls",
  "uri",
  "link",
  "path",
  "file",
  "filename",
  "prompt",
  "text",
  "input",
  "name",
];

/** For URL-like values, show just the FQDN (host) rather than the full https://… URL. */
function shorten(value: string): string {
  const v = value.trim();
  if (/^https?:\/\//i.test(v)) {
    try {
      return new URL(v).hostname.replace(/^www\./, "");
    } catch {
      /* fall through to raw value */
    }
  }
  return v;
}

function argsPreview(args: Record<string, any> | undefined): string | null {
  if (!args || typeof args !== "object") return null;
  for (const key of PREVIEW_KEYS) {
    const v = (args as any)[key];
    if (v == null) continue;
    if (typeof v === "string" && v.trim()) return shorten(v);
    if (Array.isArray(v) && v.length) {
      const first = v.find((x) => typeof x === "string" && x.trim());
      if (first) return v.length > 1 ? `${shorten(first)} +${v.length - 1}` : shorten(first);
    }
  }
  // Fall back to the first non-empty string value.
  for (const v of Object.values(args)) {
    if (typeof v === "string" && v.trim()) return shorten(v);
  }
  return null;
}

export function ToolCallCard({ call }: { call: LiveToolCall }) {
  const [open, setOpen] = useState(false);
  const statusColor =
    call.status === "ok"
      ? "text-green-700 dark:text-green-400"
      : call.status === "error"
        ? "text-red-700 dark:text-red-400"
        : "text-amber-700 dark:text-amber-400";
  const preview = argsPreview(call.arguments);
  return (
    <div className="my-1 rounded border border-gray-300 bg-gray-50 text-xs dark:border-gray-600 dark:bg-gray-800">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between gap-2 px-2 py-1 text-left"
      >
        <span className="flex min-w-0 items-center gap-1.5">
          <span className="shrink-0">
            🔧 <span className="font-mono">{call.tool}</span>
          </span>
          {preview && (
            <span
              className="min-w-0 truncate text-gray-500 dark:text-gray-400"
              title={preview}
            >
              {preview}
            </span>
          )}
        </span>
        <span className={`shrink-0 ${statusColor}`}>{call.status}</span>
      </button>
      {open && (
        <div className="space-y-1 border-t border-gray-200 px-2 py-1 dark:border-gray-700">
          <div>
            <div className="font-semibold text-gray-500 dark:text-gray-300">Arguments</div>
            <pre className="whitespace-pre-wrap">
              {JSON.stringify(call.arguments, null, 2)}
            </pre>
          </div>
          {call.result && (
            <div>
              <div className="font-semibold text-gray-500 dark:text-gray-300">Result</div>
              <pre className="max-h-40 overflow-auto whitespace-pre-wrap">
                {call.result}
              </pre>
            </div>
          )}
          {call.citations && call.citations.length > 0 && (
            <div>
              <div className="font-semibold text-gray-500 dark:text-gray-300">Citations</div>
              <ul className="list-disc pl-4">
                {call.citations.map((c: any, i: number) => (
                  <li key={i}>
                    <a
                      href={c.url}
                      target="_blank"
                      rel="noreferrer"
                      className="text-blue-500 underline"
                    >
                      {c.title || c.url}
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
