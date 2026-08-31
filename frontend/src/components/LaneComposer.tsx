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
  editing?: boolean;
  onCancelEdit?: () => void;
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
  editing = false,
  onCancelEdit,
  autoFocusKey,
  leftAccessory,
  starters,
  onSend,
}: Props) {
  const [text, setText] = useState("");
  const [target, setTarget] = useState<string>("all");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const responders = lanes.filter((l) => l.role === "responder");

  // Prefill when an "Edit & resend" is triggered.
  useEffect(() => {
    if (initialText !== undefined) setText(initialText);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialTextKey]);

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
    // Preserve the current response and send this payload when its target lanes are free.
    if (streaming && onEnqueue && !editing) {
      enqueuePayload(payload);
      clearComposer();
      return;
    }
    onSend(content, payload.attachments.map((a) => a.id), payload.targetLaneIds);
    clearComposer();
  }

  function enqueuePayload(p: Omit<QueuedMessage, "id">) {
    onEnqueue?.({ id: crypto.randomUUID(), ...p });
  }

  const targetLane = responders.find((l) => l.id === target);
  const placeholder = targetLane
    ? `Message ${targetLane.model} only…`
    : `Message all lanes…`;

  return (
    <div className="border-t border-gray-200 bg-white p-1.5 pb-[max(0.375rem,env(safe-area-inset-bottom))] lg:p-2 dark:border-gray-700 dark:bg-gray-900">
      <div className="group w-full lg:px-1">
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
      <div>
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
        {editing && (
          <button
            type="button"
            onClick={() => {
              clearComposer();
              onCancelEdit?.();
            }}
            title="Cancel editing and keep the original turn"
            aria-label="Cancel editing"
            className="inline-flex min-h-11 shrink-0 items-center justify-center rounded border border-gray-300 px-2 text-sm text-gray-600 hover:bg-gray-100 lg:px-3 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
          >
            <span className="lg:hidden">✕</span>
            <span className="hidden lg:inline">Cancel edit</span>
          </button>
        )}
        <label className="sr-only" htmlFor="composer-target">
          Which lanes to send to
        </label>
        <select
          id="composer-target"
          value={target}
          onChange={(e) => setTarget(e.target.value)}
          title="Which lanes to send to"
          className="min-h-11 w-20 rounded border border-gray-300 px-1 py-1.5 text-sm lg:hidden dark:border-gray-600 dark:bg-gray-800"
        >
          <option value="all">All</option>
          {responders.map((l) => (
            <option key={l.id} value={l.id}>
              {l.model}
            </option>
          ))}
        </select>
        <select
          value={target}
          onChange={(e) => setTarget(e.target.value)}
          title="Which lanes to send to"
          aria-label="Which lanes to send to"
          className="hidden rounded border border-gray-300 px-2 py-1.5 text-sm lg:block dark:border-gray-600 dark:bg-gray-800"
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
            className="min-h-11 rounded bg-blue-600 px-5 py-1.5 text-sm font-medium text-white disabled:opacity-40 lg:min-h-0 lg:px-4"
          >
            Send
          </button>
        </div>
            </>
          }
        />
      </div>
      <div className="mt-1 hidden px-1 text-[11px] text-gray-500 opacity-0 transition-opacity group-focus-within:opacity-100 lg:block dark:text-gray-400">
        Enter to send · Shift+Enter for newline · paste or drop an image to attach
      </div>
      </div>
    </div>
  );
}
