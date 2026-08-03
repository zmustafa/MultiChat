import { useEffect, useState, type ReactNode } from "react";
import type { Attachment, Lane } from "../api/types";
import { PromptField } from "./ComposerExtras";

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
  /** Example prompts offered above the box while the topic has no turns yet. */
  starters?: string[];
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
  starters,
  onSend,
}: Props) {
  const [text, setText] = useState("");
  const [target, setTarget] = useState<string>("all");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [pending, setPending] = useState<Omit<QueuedMessage, "id"> | null>(null);
  const responders = lanes.filter((l) => l.role === "responder");

  // Prefill when an "Edit & resend" is triggered.
  useEffect(() => {
    if (initialText !== undefined) setText(initialText);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialTextKey]);

  // Dismiss the send/queue popup with Escape.
  useEffect(() => {
    if (!pending) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setPending(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [pending]);

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

  const targetLane = responders.find((l) => l.id === target);
  const placeholder = targetLane
    ? `Message ${targetLane.model} only…`
    : `Message all lanes…`;

  return (
    <div className="border-t border-gray-200 bg-white p-2 dark:border-gray-700 dark:bg-gray-900">
      <div className="group w-full px-1">
      {starters && starters.length > 0 && (
        <div className="mb-2 flex items-center gap-1.5 overflow-x-auto pb-0.5">
          <span className="shrink-0 text-[11px] font-medium text-gray-500 dark:text-gray-400">
            Examples:
          </span>
          {starters.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => setText(s)}
              title="Put this prompt in the composer"
              className="shrink-0 whitespace-nowrap rounded-full border border-gray-300 px-2.5 py-1 text-xs text-gray-600 transition hover:border-brand hover:text-brand dark:border-gray-600 dark:text-gray-300"
            >
              {s}
            </button>
          ))}
        </div>
      )}
      <div className="flex items-end gap-2">
        <PromptField
          value={text}
          onChange={setText}
          onSubmit={submit}
          attachments={attachments}
          onAttachmentsChange={setAttachments}
          focusKey={autoFocusKey}
          placeholder={placeholder}
          ariaLabel={`Prompt — ${placeholder}`}
          disabled={disabled}
          footer={leftAccessory}
          trailing={
            <>
        <label className="sr-only" htmlFor="composer-target">
          Which lanes to send to
        </label>
        <select
          id="composer-target"
          value={target}
          onChange={(e) => setTarget(e.target.value)}
          title="Which lanes to send to"
          className="rounded border border-gray-300 px-2 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800"
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
            </>
          }
        />
      </div>
      <div className="mt-1 px-1 text-[11px] text-gray-500 opacity-0 transition-opacity group-focus-within:opacity-100 dark:text-gray-400">
        Enter to send · Shift+Enter for newline · paste or drop an image to attach
      </div>
      </div>
    </div>
  );
}
