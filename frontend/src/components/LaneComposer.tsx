import { useEffect, useRef, useState, type ReactNode } from "react";
import { apiFetch, mediaUrl } from "../api/client";
import type { Attachment, Lane } from "../api/types";
import { useSnippets } from "../hooks/useExtras";
import { useDismiss } from "../hooks/useDismiss";
import { ScreenshotCapture } from "./ScreenshotCapture";

interface Props {
  lanes: Lane[];
  disabled: boolean;
  streaming?: boolean;
  queue?: QueuedMessage[];
  onEnqueue?: (msg: QueuedMessage) => void;
  onRemoveQueued?: (id: string) => void;
  initialText?: string;
  initialTextKey?: number;
  /** Focus the prompt box whenever this changes (e.g. the active chat id). */
  autoFocusKey?: number | string;
  /** Optional control rendered inline in the send row (left of the target selector). */
  leftAccessory?: ReactNode;
  onSend: (
    content: string,
    attachmentIds: string[],
    targetLaneIds?: string[]
  ) => void;
}

export interface QueuedMessage {
  id: string;
  content: string;
  attachments: Attachment[];
  targetLaneIds?: string[];
}

export function LaneComposer({
  lanes,
  disabled,
  streaming,
  onEnqueue,
  initialText,
  initialTextKey,
  autoFocusKey,
  leftAccessory,
  onSend,
}: Props) {
  const [text, setText] = useState("");
  const [target, setTarget] = useState<string>("all");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [listening, setListening] = useState(false);
  const [plusOpen, setPlusOpen] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [captureOpen, setCaptureOpen] = useState(false);
  const [pending, setPending] = useState<Omit<QueuedMessage, "id"> | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const recogRef = useRef<any>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const plusRef = useRef<HTMLDivElement>(null);
  const { data: snippets = [] } = useSnippets();

  const responders = lanes.filter((l) => l.role === "responder");

  useDismiss(plusRef, plusOpen, () => setPlusOpen(false));

  // Prefill when an "Edit & resend" is triggered.
  useEffect(() => {
    if (initialText !== undefined) setText(initialText);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialTextKey]);

  // Auto-focus the prompt box whenever focus is brought to a chat (the active chat id
  // changes — opening, switching, or starting a chat), so the user can type right away.
  useEffect(() => {
    if (autoFocusKey === undefined) return;
    textareaRef.current?.focus();
  }, [autoFocusKey]);

  // Auto-grow the prompt box with its content, up to 8 lines; then it scrolls internally.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    const cs = getComputedStyle(el);
    const lh = parseFloat(cs.lineHeight) || 20;
    const padY =
      (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0);
    const max = lh * 8 + padY;
    el.style.height = Math.min(el.scrollHeight, max) + "px";
    el.style.overflowY = el.scrollHeight > max ? "auto" : "hidden";
  }, [text]);

  // Dismiss the send/queue popup with Escape.
  useEffect(() => {
    if (!pending) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setPending(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [pending]);

  function toggleVoice() {
    const SR =
      (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
    if (!SR) {
      alert("Voice input isn't supported in this browser.");
      return;
    }
    if (listening) {
      recogRef.current?.stop();
      setListening(false);
      return;
    }
    const r = new SR();
    r.lang = "en-US";
    r.interimResults = true;
    r.continuous = true;
    // Text already in the box before recording started. Finalized speech is appended to it,
    // so pausing between sentences accumulates rather than replacing what was said before.
    let committed = text.trim();
    r.onresult = (e: any) => {
      let interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const res = e.results[i];
        const chunk = res[0].transcript;
        if (res.isFinal) {
          committed = (committed ? committed + " " : "") + chunk.trim();
        } else {
          interim += chunk;
        }
      }
      const preview = interim.trim();
      setText(committed + (preview ? (committed ? " " : "") + preview : ""));
    };
    r.onend = () => setListening(false);
    r.onerror = () => setListening(false);
    recogRef.current = r;
    r.start();
    setListening(true);
  }

  async function uploadFiles(files: File[]) {
    const ok = files.filter(
      (f) =>
        f.type.startsWith("image/") ||
        /\.(pdf|docx|xlsx|csv|txt|md)$/i.test(f.name) ||
        [
          "application/pdf",
          "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          "text/csv",
          "text/plain",
          "text/markdown",
        ].includes(f.type)
    );
    if (ok.length === 0) return;
    setUploading(true);
    try {
      const form = new FormData();
      ok.forEach((f) => form.append("files", f));
      const res = await apiFetch<Attachment[]>("/api/uploads", {
        method: "POST",
        body: form,
      });
      setAttachments((prev) => [...prev, ...res]);
    } catch (e) {
      alert((e as Error).message);
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  async function handlePaste(e: React.ClipboardEvent<HTMLTextAreaElement>) {
    const items = e.clipboardData?.items;
    if (!items) return;
    const files: File[] = [];
    for (const item of items) {
      if (item.kind === "file") {
        const f = item.getAsFile();
        if (f) files.push(f);
      }
    }
    if (files.length > 0) {
      e.preventDefault();
      await uploadFiles(files); // uploadFiles filters to accepted image/doc types
    }
  }

  function handleDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragOver(false);
    const files = Array.from(e.dataTransfer?.files ?? []);
    if (files.length) uploadFiles(files);
  }

  function clearComposer() {
    setText("");
    setAttachments([]);
  }

  function submit() {
    const content = text.trim();
    if (!content && attachments.length === 0) return;
    const payload = {
      content,
      attachments: [...attachments],
      targetLaneIds: target === "all" ? undefined : [target],
    };
    // A response is still generating — ask whether to interrupt or enqueue.
    if (streaming && onEnqueue) {
      setPending(payload);
      return;
    }
    onSend(content, payload.attachments.map((a) => a.id), payload.targetLaneIds);
    clearComposer();
  }

  function sendPayloadNow(p: Omit<QueuedMessage, "id">) {
    onSend(p.content, p.attachments.map((a) => a.id), p.targetLaneIds);
  }

  function enqueuePayload(p: Omit<QueuedMessage, "id">) {
    onEnqueue?.({ id: crypto.randomUUID(), ...p });
  }

  return (
    <div className="border-t border-gray-200 bg-white p-2 dark:border-gray-700 dark:bg-gray-900">
      <div className="flex items-end gap-2">
        <div
          className="relative flex flex-1 flex-col gap-1.5 rounded-lg border border-gray-300 bg-white px-2 py-1.5 focus-within:border-brand dark:border-gray-600 dark:bg-gray-800"
          onMouseDown={(e) => {
            // Click anywhere in the field (padding, toolbar gap, etc.) focuses the prompt —
            // except when pressing an actual control (+ menu, attachment ×, buttons, select).
            const el = e.target as HTMLElement;
            if (el.closest("button, a, input, select, textarea, label")) return;
            e.preventDefault(); // keep focus on the textarea instead of the clicked div
            textareaRef.current?.focus();
          }}
          onDragOver={(e) => {
            e.preventDefault();
            if (!dragOver) setDragOver(true);
          }}
          onDragLeave={(e) => {
            if (!e.currentTarget.contains(e.relatedTarget as Node)) setDragOver(false);
          }}
          onDrop={handleDrop}
        >
          {dragOver && (
            <div className="pointer-events-none absolute inset-0 z-20 flex items-center justify-center rounded-lg border-2 border-dashed border-brand bg-brand/10 text-sm font-medium text-brand">
              Drop files to attach
            </div>
          )}
          {attachments.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {attachments.map((a) =>
                a.kind === "document" ? (
                  <div
                    key={a.id}
                    className="relative flex items-center gap-1 rounded-lg border border-gray-200 bg-gray-50 px-2 py-1.5 text-xs dark:border-gray-600 dark:bg-gray-900"
                    title={a.filename}
                  >
                    <span className="text-base">📄</span>
                    <span className="max-w-[140px] truncate">{a.filename}</span>
                    <button
                      onClick={() =>
                        setAttachments((prev) => prev.filter((x) => x.id !== a.id))
                      }
                      className="ml-1 flex h-4 w-4 items-center justify-center rounded-full bg-gray-700/80 text-[11px] leading-none text-white hover:bg-red-500"
                    >
                      ×
                    </button>
                  </div>
                ) : (
                  <div key={a.id} className="relative">
                    <img
                      src={mediaUrl(a.url)}
                      alt={a.filename}
                      className="h-16 w-16 rounded-lg border border-gray-200 object-cover dark:border-gray-600"
                    />
                    <button
                      onClick={() =>
                        setAttachments((prev) => prev.filter((x) => x.id !== a.id))
                      }
                      className="absolute -right-1.5 -top-1.5 flex h-5 w-5 items-center justify-center rounded-full bg-gray-800 text-xs leading-none text-white shadow hover:bg-red-500"
                    >
                      ×
                    </button>
                  </div>
                )
              )}
            </div>
          )}
          <textarea
            ref={textareaRef}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onPaste={handlePaste}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
            placeholder="Type a prompt… (Enter to send, Shift+Enter for newline, paste an image)"
            rows={1}
            className="block w-full resize-none overflow-hidden bg-transparent text-sm leading-5 focus:outline-none"
          />
          {/* Bottom-left “+” menu: consolidates the available add/attach actions. */}
          <div className="flex items-center gap-2">
            <div className="relative" ref={plusRef}>
              <button
                type="button"
                onClick={() => setPlusOpen((o) => !o)}
                title="Add attachment or action"
                className="flex h-7 w-7 items-center justify-center rounded-full border border-gray-300 text-lg leading-none text-gray-600 hover:bg-gray-100 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
              >
                +
              </button>
              {plusOpen && (
                <div className="absolute bottom-9 left-0 z-30 max-h-72 w-60 overflow-y-auto rounded-lg border border-gray-200 bg-white py-1 shadow-xl dark:border-gray-700 dark:bg-gray-900">
                  <button
                    type="button"
                    onClick={() => {
                      fileRef.current?.click();
                      setPlusOpen(false);
                    }}
                    className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-800"
                  >
                    <span>📎</span> Add photos &amp; files
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setCaptureOpen(true);
                      setPlusOpen(false);
                    }}
                    className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-800"
                  >
                    <span>📸</span> Capture screenshot
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      toggleVoice();
                      setPlusOpen(false);
                    }}
                    className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-800"
                  >
                    <span>{listening ? "⏺" : "🎤"}</span>{" "}
                    {listening ? "Stop voice input" : "Voice input"}
                  </button>
                  {snippets.length > 0 && (
                    <>
                      <div className="mt-1 border-t border-gray-100 px-3 pb-0.5 pt-1 text-[10px] font-semibold uppercase tracking-wide text-gray-400 dark:border-gray-800">
                        Snippets
                      </div>
                      {snippets.map((s) => (
                        <button
                          key={s.id}
                          type="button"
                          onClick={() => {
                            setText((t) => (t ? t + "\n" : "") + s.content);
                            setPlusOpen(false);
                          }}
                          className="block w-full px-3 py-1.5 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-800"
                          title={s.content}
                        >
                          <span className="block truncate font-medium">⚡ {s.title}</span>
                          <span className="block truncate text-[11px] text-gray-400">
                            {s.content}
                          </span>
                        </button>
                      ))}
                    </>
                  )}
                </div>
              )}
            </div>
            {listening && (
              <span className="flex items-center gap-1 text-xs text-red-500">
                <span className="h-2 w-2 animate-pulse rounded-full bg-red-500" />
                listening…
              </span>
            )}
          </div>
        </div>
        <input
          ref={fileRef}
          type="file"
          accept="image/png,image/jpeg,image/webp,image/gif,application/pdf,.pdf,.docx,.xlsx,.csv,.txt,.md"
          multiple
          hidden
          onChange={(e) => uploadFiles(Array.from(e.target.files ?? []))}
        />
        {leftAccessory}
        <select
          value={target}
          onChange={(e) => setTarget(e.target.value)}
          title="Which lanes to send to"
          className="rounded border border-gray-300 px-2 py-1 text-sm dark:border-gray-600 dark:bg-gray-800"
        >
          <option value="all">Broadcast to all</option>
          {responders.map((l) => (
            <option key={l.id} value={l.id}>
              {l.model}
            </option>
          ))}
        </select>
        <div className="relative">
          <button
            onClick={submit}
            disabled={disabled}
            className="rounded bg-blue-600 px-4 py-1.5 text-sm font-medium text-white disabled:opacity-40"
          >
            Send
          </button>
          {pending && (
            <div className="absolute bottom-full right-0 z-30 mb-2 w-72 overflow-hidden rounded-lg border border-gray-200 bg-white text-left shadow-xl dark:border-gray-700 dark:bg-gray-900">
              <div className="border-b border-gray-100 px-3 py-2 text-[11px] text-gray-500 dark:border-gray-800">
                A response is still generating. What would you like to do?
              </div>
              <button
                onClick={() => {
                  sendPayloadNow(pending);
                  clearComposer();
                  setPending(null);
                }}
                className="flex w-full items-start gap-2 px-3 py-2 text-left hover:bg-gray-100 dark:hover:bg-gray-800"
              >
                <span>⚡</span>
                <span>
                  <span className="block text-sm font-medium">Send now</span>
                  <span className="block text-[11px] text-gray-400">
                    Interrupt the current response and send immediately
                  </span>
                </span>
              </button>
              <button
                onClick={() => {
                  enqueuePayload(pending);
                  clearComposer();
                  setPending(null);
                }}
                className="flex w-full items-start gap-2 px-3 py-2 text-left hover:bg-gray-100 dark:hover:bg-gray-800"
              >
                <span>➕</span>
                <span>
                  <span className="block text-sm font-medium">Add to queue</span>
                  <span className="block text-[11px] text-gray-400">
                    Send automatically when the current response finishes
                  </span>
                </span>
              </button>
              <button
                onClick={() => setPending(null)}
                className="w-full border-t border-gray-100 px-3 py-1.5 text-left text-xs text-gray-500 hover:bg-gray-100 dark:border-gray-800 dark:hover:bg-gray-800"
              >
                Cancel <span className="text-gray-400">(Esc)</span>
              </button>
            </div>
          )}
        </div>
      </div>
      {captureOpen && (
        <ScreenshotCapture
          onAttach={(file) => uploadFiles([file])}
          onClose={() => setCaptureOpen(false)}
        />
      )}
    </div>
  );
}
