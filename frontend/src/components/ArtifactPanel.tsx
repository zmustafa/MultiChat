import { removeArtifact, useArtifacts } from "../hooks/useArtifacts";
import { MessageRenderer } from "./MessageRenderer";

export function ArtifactPanel({ onClose }: { onClose: () => void }) {
  const artifacts = useArtifacts();
  return (
    <div className="flex w-96 shrink-0 flex-col border-l border-gray-200 bg-gray-50 dark:border-gray-700 dark:bg-gray-950">
      <div className="flex items-center justify-between border-b border-gray-200 px-3 py-2 dark:border-gray-700">
        <span className="text-sm font-semibold">📌 Artifacts ({artifacts.length})</span>
        <button onClick={onClose} className="text-xs text-gray-500 hover:text-gray-800">
          ✕
        </button>
      </div>
      <div className="flex-1 space-y-3 overflow-y-auto p-2">
        {artifacts.length === 0 && (
          <div className="p-3 text-xs text-gray-500">
            Pin a response with the 📌 button to collect it here.
          </div>
        )}
        {artifacts.map((a) => (
          <div
            key={a.id}
            className="rounded-lg border border-gray-200 bg-white p-2 dark:border-gray-700 dark:bg-gray-900"
          >
            <div className="mb-1 flex items-center justify-between gap-2">
              <span className="truncate text-xs font-medium">{a.title}</span>
              <span className="flex shrink-0 gap-1">
                <button
                  onClick={() => navigator.clipboard.writeText(a.content)}
                  className="text-[10px] text-gray-400 hover:text-gray-700"
                >
                  Copy
                </button>
                <button
                  onClick={() => removeArtifact(a.id)}
                  className="text-[10px] text-gray-400 hover:text-red-500"
                >
                  Remove
                </button>
              </span>
            </div>
            <div className="max-h-64 overflow-y-auto">
              <MessageRenderer content={a.content} />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
