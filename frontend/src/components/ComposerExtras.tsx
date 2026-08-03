import { useEffect, useRef, useState } from "react";
import { apiFetch, mediaUrl } from "../api/client";
import type { Attachment } from "../api/types";
import { useDismiss } from "../hooks/useDismiss";
import { useSnippets } from "../hooks/useExtras";
import { ScreenshotCapture } from "./ScreenshotCapture";

/**
 * Dictation for a prompt box.
 *
 * Finalized speech is accumulated into `committed` rather than rebuilt from the current
 * result index, so pausing between sentences appends instead of discarding what came
 * before — the bug that made long dictations lose their opening lines.
 */
export function useVoiceInput(
  text: string,
  setText: (updater: (previous: string) => string) => void,
) {
  const [listening, setListening] = useState(false);
  const recogRef = useRef<any>(null);

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
    let committed = text.trim();
    r.onresult = (e: any) => {
      let interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const res = e.results[i];
        const chunk = res[0].transcript;
        if (res.isFinal) committed = (committed ? committed + " " : "") + chunk.trim();
        else interim += chunk;
      }
      const preview = interim.trim();
      setText(() => committed + (preview ? (committed ? " " : "") + preview : ""));
    };
    r.onend = () => setListening(false);
    r.onerror = () => setListening(false);
    recogRef.current = r;
    r.start();
    setListening(true);
  }

  return { listening, toggleVoice };
}

/**
 * The composer's "+" menu: attach files, capture a screenshot, dictate, drop in a snippet.
 *
 * Shared so the deliberation launcher offers exactly what the chat composer offers.
 */
export function AttachMenu({
  onPickFiles,
  onCapture,
  onVoice,
  listening,
  onSnippet,
  align = "bottom",
}: {
  onPickFiles: () => void;
  onCapture: () => void;
  onVoice: () => void;
  listening: boolean;
  onSnippet: (content: string) => void;
  /** Where the menu opens relative to the button. */
  align?: "bottom" | "top";
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const { data: snippets = [] } = useSnippets();
  useDismiss(ref, open, () => setOpen(false));

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        title="Add attachment or action"
        className="flex h-7 w-7 items-center justify-center rounded-full border border-gray-300 text-lg leading-none text-gray-600 hover:bg-gray-100 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
      >
        +
      </button>
      {open && (
        <div
          className={`absolute left-0 z-30 max-h-72 w-60 overflow-y-auto rounded-lg border border-gray-200 bg-white py-1 shadow-xl dark:border-gray-700 dark:bg-gray-900 ${
            align === "top" ? "bottom-9" : "top-9"
          }`}
        >
          <button
            type="button"
            onClick={() => {
              onPickFiles();
              setOpen(false);
            }}
            className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-800"
          >
            <span>📎</span> Add photos &amp; files
          </button>
          <button
            type="button"
            onClick={() => {
              onCapture();
              setOpen(false);
            }}
            className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-800"
          >
            <span>📸</span> Capture screenshot
          </button>
          <button
            type="button"
            onClick={() => {
              onVoice();
              setOpen(false);
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
                    onSnippet(s.content);
                    setOpen(false);
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
  );
}

/** File types the uploader accepts — images for vision, documents for grounding. */
export const ATTACH_ACCEPT =
  "image/png,image/jpeg,image/webp,image/gif,application/pdf,.pdf,.docx,.xlsx,.csv,.txt,.md";

/** Keep only files the backend can actually use (images, or extractable documents). */
export function acceptedFiles(files: File[]): File[] {
  return files.filter(
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
      ].includes(f.type),
  );
}

/**
 * A prompt box with everything a prompt box is expected to do: paste, drag-and-drop,
 * attach, screenshot, dictation, snippets, thumbnails and auto-grow.
 *
 * It exists because those behaviours were re-implemented per screen and immediately
 * drifted — a follow-up box that silently swallows a pasted screenshot looks broken even
 * though every individual feature "exists" somewhere in the app. One component, one
 * behaviour, everywhere it is mounted.
 */
export function PromptField({
  value,
  onChange,
  onSubmit,
  attachments,
  onAttachmentsChange,
  placeholder,
  ariaLabel,
  submitOn = "enter",
  autoFocus,
  focusKey,
  minRows = 1,
  maxLines = 8,
  disabled,
  footer,
  trailing,
}: {
  value: string;
  onChange: (next: string) => void;
  onSubmit?: () => void;
  attachments: Attachment[];
  onAttachmentsChange: (next: Attachment[]) => void;
  placeholder?: string;
  /** Accessible name for the textarea when the placeholder alone isn't descriptive. */
  ariaLabel?: string;
  /** "enter" sends on Enter; "mod-enter" needs Ctrl/Cmd (for long-form question boxes). */
  submitOn?: "enter" | "mod-enter";
  autoFocus?: boolean;
  /** Focus the box whenever this value changes (switching chats, for instance). */
  focusKey?: number | string;
  minRows?: number;
  maxLines?: number;
  disabled?: boolean;
  /** Rendered under the field, next to the + menu. */
  footer?: React.ReactNode;
  /** Rendered to the right of the field (a Send button, typically). */
  trailing?: React.ReactNode;
}) {
  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [captureOpen, setCaptureOpen] = useState(false);
  const [error, setError] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const { listening, toggleVoice } = useVoiceInput(value, (update) =>
    onChange(update(value)),
  );

  // Grow with the content up to `maxLines`, then scroll internally.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    const cs = getComputedStyle(el);
    const lh = parseFloat(cs.lineHeight) || 20;
    const padY = (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0);
    const max = lh * maxLines + padY;
    el.style.height = Math.min(el.scrollHeight, max) + "px";
    el.style.overflowY = el.scrollHeight > max ? "auto" : "hidden";
  }, [value, maxLines]);

  // Focus when the host says the context changed — e.g. switching chats.
  useEffect(() => {
    if (focusKey === undefined) return;
    textareaRef.current?.focus();
  }, [focusKey]);

  async function upload(files: File[]) {
    const ok = acceptedFiles(files);
    if (!ok.length) return;
    setUploading(true);
    setError("");
    try {
      const form = new FormData();
      ok.forEach((f) => form.append("files", f));
      const res = await apiFetch<Attachment[]>("/api/uploads", {
        method: "POST",
        body: form,
      });
      onAttachmentsChange([...attachments, ...res]);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  function filesFromClipboard(e: React.ClipboardEvent): File[] {
    return Array.from(e.clipboardData?.items ?? [])
      .filter((i) => i.kind === "file")
      .map((i) => i.getAsFile())
      .filter((f): f is File => !!f);
  }

  return (
    <div className="flex min-w-0 flex-1 flex-col">
      <div className="flex items-end gap-2">
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragOver(true);
          }}
          onDragLeave={(e) => {
            if (!e.currentTarget.contains(e.relatedTarget as Node)) setDragOver(false);
          }}
          onDrop={(e) => {
            e.preventDefault();
            setDragOver(false);
            void upload(Array.from(e.dataTransfer?.files ?? []));
          }}
          onMouseDown={(e) => {
            // Clicking the padding or the toolbar gap should still focus the prompt.
            if ((e.target as HTMLElement).closest("button,a,input,select,textarea,label")) return;
            e.preventDefault();
            textareaRef.current?.focus();
          }}
          className={`relative flex flex-1 flex-col rounded-lg border ${
            dragOver ? "border-dashed border-brand" : "border-gray-300 dark:border-gray-600"
          } px-2 py-1.5 focus-within:border-brand dark:bg-gray-800`}
        >
          {attachments.length > 0 && (
            <div className="flex flex-wrap gap-2 pb-1.5">
              {attachments.map((a) => (
                <div key={a.id} className="relative">
                  {a.kind === "image" ? (
                    <img
                      src={mediaUrl(a.url)}
                      alt={a.filename}
                      title={a.filename}
                      className="h-16 rounded border border-gray-200 object-cover dark:border-gray-700"
                    />
                  ) : (
                    <span className="flex items-center gap-1 rounded border border-gray-200 bg-gray-50 px-2 py-1 text-[11px] text-gray-600 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300">
                      📄 <span className="max-w-[12rem] truncate">{a.filename}</span>
                    </span>
                  )}
                  <button
                    onClick={() =>
                      onAttachmentsChange(attachments.filter((x) => x.id !== a.id))
                    }
                    title="Remove"
                    className="absolute -right-1.5 -top-1.5 rounded-full bg-gray-700 px-1 text-[10px] text-white"
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          )}

          <textarea
            ref={textareaRef}
            autoFocus={autoFocus}
            value={value}
            disabled={disabled}
            rows={minRows}
            placeholder={placeholder}
            aria-label={ariaLabel ?? placeholder ?? "Prompt"}
            onChange={(e) => onChange(e.target.value)}
            onPaste={(e) => {
              const files = filesFromClipboard(e);
              if (files.length) {
                e.preventDefault();
                void upload(files);
              }
            }}
            onKeyDown={(e) => {
              if (e.key !== "Enter" || !onSubmit) return;
              const wanted = submitOn === "enter" ? !e.shiftKey : e.metaKey || e.ctrlKey;
              if (wanted) {
                e.preventDefault();
                onSubmit();
              }
            }}
            className="block w-full resize-none overflow-hidden bg-transparent text-sm leading-5 focus:outline-none"
          />

          <div className="flex items-center gap-2 pt-1">
            <AttachMenu
              onPickFiles={() => fileRef.current?.click()}
              onCapture={() => setCaptureOpen(true)}
              onVoice={toggleVoice}
              listening={listening}
              onSnippet={(content) => onChange(value ? `${value}\n${content}` : content)}
            />
            {uploading && <span className="text-[11px] text-gray-400">Uploading…</span>}
            {listening && (
              <span className="flex items-center gap-1 text-[11px] text-red-500">
                <span className="h-2 w-2 animate-pulse rounded-full bg-red-500" />
                listening…
              </span>
            )}
            {footer}
          </div>

          {dragOver && (
            <div className="pointer-events-none absolute inset-0 flex items-center justify-center rounded-lg bg-brand/5 text-xs text-brand">
              Drop images or documents to attach
            </div>
          )}
        </div>
        {trailing}
      </div>

      <input
        ref={fileRef}
        type="file"
        accept={ATTACH_ACCEPT}
        multiple
        hidden
        onChange={(e) => void upload(Array.from(e.target.files ?? []))}
      />
      {error && <div className="mt-1 text-[11px] text-rose-500">{error}</div>}
      {captureOpen && (
        <ScreenshotCapture
          onAttach={(file) => upload([file])}
          onClose={() => setCaptureOpen(false)}
        />
      )}
    </div>
  );
}

