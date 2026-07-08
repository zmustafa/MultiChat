import { useRef, useState } from "react";
import type { Lane, LaneMessage, Turn } from "../api/types";
import type { LiveLane } from "../hooks/useBroadcast";
import { MessageRenderer } from "./MessageRenderer";

interface Props {
  judgeLane: Lane | undefined;
  latestTurn: Turn | undefined;
  messages: LaneMessage[];
  live?: LiveLane;
  streaming: boolean;
  onRun: (turnId: string) => void;
}

export function JudgePanel({
  judgeLane,
  latestTurn,
  messages,
  live,
  streaming,
  onRun,
}: Props) {
  const contentRef = useRef<HTMLDivElement>(null);
  const [copied, setCopied] = useState(false);
  if (!judgeLane) return null;
  const judgeMsg = messages
    .filter((m) => m.lane_id === judgeLane.id && m.turn_id === latestTurn?.id)
    .at(-1);

  const isLive = !!live && (live.status === "streaming" || live.status === "queued");
  const content = isLive ? live!.content || "" : judgeMsg?.content || "";
  const hasAnswer = !isLive && !!content.trim();

  function copyAnswer() {
    navigator.clipboard.writeText(content);
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  }

  function downloadMarkdown() {
    const blob = new Blob([content], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "judge-answer.md";
    a.click();
    URL.revokeObjectURL(url);
  }

  function exportPdf() {
    const html = contentRef.current?.innerHTML;
    if (!html) return;
    const w = window.open("", "_blank");
    if (!w) return;
    w.document.write(
      `<!doctype html><html><head><title>Judge — best synthesized answer</title>` +
        `<style>body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:720px;` +
        `margin:2rem auto;padding:0 1.5rem;line-height:1.55;color:#111}` +
        `pre{background:#f4f4f5;padding:10px 12px;border-radius:8px;overflow:auto}` +
        `code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.9em}` +
        `table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:4px 8px}` +
        `h1,h2,h3{line-height:1.25}img{max-width:100%}</style></head><body>` +
        `${html}</body></html>`
    );
    w.document.close();
    w.focus();
    w.print();
  }

  return (
    <div className="border-t border-gray-200 bg-amber-50 p-3 dark:border-gray-700 dark:bg-amber-950/30">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="text-sm font-semibold">⚖️ Judge — {judgeLane.model}</div>
        <div className="flex items-center gap-1.5">
          {hasAnswer && (
            <>
              <button
                onClick={copyAnswer}
                title="Copy the synthesized answer"
                className="rounded border border-amber-300 px-2 py-1 text-xs text-amber-800 hover:bg-amber-100 dark:border-amber-800 dark:text-amber-200 dark:hover:bg-amber-900/40"
              >
                {copied ? "✓ Copied" : "Copy"}
              </button>
              <button
                onClick={downloadMarkdown}
                title="Download as Markdown (.md)"
                className="rounded border border-amber-300 px-2 py-1 text-xs text-amber-800 hover:bg-amber-100 dark:border-amber-800 dark:text-amber-200 dark:hover:bg-amber-900/40"
              >
                ⬇ .md
              </button>
              <button
                onClick={exportPdf}
                title="Export as PDF (print dialog)"
                className="rounded border border-amber-300 px-2 py-1 text-xs text-amber-800 hover:bg-amber-100 dark:border-amber-800 dark:text-amber-200 dark:hover:bg-amber-900/40"
              >
                ⬇ PDF
              </button>
            </>
          )}
          <button
            disabled={!latestTurn || streaming}
            onClick={() => latestTurn && onRun(latestTurn.id)}
            className="rounded bg-amber-700 px-3 py-1 text-xs font-medium text-white hover:bg-amber-800 disabled:opacity-40"
          >
            Synthesize best answer
          </button>
        </div>
      </div>
      <div className="max-h-56 overflow-y-auto" ref={contentRef}>
        {isLive ? (
          <MessageRenderer content={live!.content || "…"} />
        ) : judgeMsg ? (
          <MessageRenderer content={judgeMsg.content} />
        ) : (
          <div className="text-xs text-gray-500">
            Run the judge to merge all lane answers into a best answer.
          </div>
        )}
      </div>
    </div>
  );
}
