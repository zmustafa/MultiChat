import { useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import type { GeneratedFile } from "../api/types";
import { AuthenticatedDownloadLink } from "./AuthenticatedMedia";

const ICONS: Record<string, string> = {
  pptx: "📊",
  docx: "📝",
  xlsx: "📈",
  pdf: "📕",
  md: "📄",
  txt: "📄",
};

function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function FilesPanel({
  sessionId,
  onClose,
}: {
  sessionId: string;
  onClose: () => void;
}) {
  const [files, setFiles] = useState<GeneratedFile[]>([]);
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    try {
      const rows = await apiFetch<GeneratedFile[]>(
        `/api/files/session/${sessionId}`
      );
      setFiles(rows);
    } catch {
      setFiles([]);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  async function remove(f: GeneratedFile) {
    if (!confirm(`Delete ${f.download_name}?`)) return;
    try {
      await apiFetch(`/api/files/${f.stored_name}`, { method: "DELETE" });
      setFiles((prev) => prev.filter((x) => x.id !== f.id));
    } catch (e) {
      alert((e as Error).message);
    }
  }

  return (
    <div className="fixed inset-x-0 bottom-0 top-11 z-40 overflow-y-auto border-b border-gray-200 bg-gray-50 px-3 py-2 lg:static lg:overflow-visible dark:border-gray-700 dark:bg-gray-900/40">
      <div className="mb-1 flex items-center justify-between">
        <span className="text-xs font-semibold text-gray-600 dark:text-gray-300">
          📁 Generated files {files.length > 0 && `(${files.length})`}
        </span>
        <div className="flex gap-2">
          <button
            onClick={load}
            className="text-xs text-gray-400 hover:text-brand"
            title="Refresh"
          >
            ↻
          </button>
          <button
            onClick={onClose}
            className="inline-flex h-11 w-11 items-center justify-center text-lg text-gray-400 hover:text-red-500 lg:h-auto lg:w-auto lg:text-xs"
            title="Close"
          >
            ✕
          </button>
        </div>
      </div>
      {loading ? (
        <div className="py-2 text-xs text-gray-400">Loading…</div>
      ) : files.length === 0 ? (
        <div className="py-2 text-xs text-gray-400">
          No files generated yet. Ask a model to create a PowerPoint, Word, Excel,
          or PDF file, or export the comparison.
        </div>
      ) : (
        <div className="flex flex-wrap gap-2">
          {files.map((f) => (
            <div
              key={f.id}
              className="flex items-center gap-2 rounded border border-gray-300 bg-white px-2 py-1 text-xs dark:border-gray-600 dark:bg-gray-800"
            >
              <span>{ICONS[f.kind] || "📎"}</span>
              <AuthenticatedDownloadLink
                href={f.url}
                filename={f.download_name}
                className="max-w-[180px] truncate text-brand hover:underline"
                title={f.download_name}
              >
                {f.download_name}
              </AuthenticatedDownloadLink>
              <span className="text-gray-400">{fmtSize(f.size_bytes)}</span>
              <button
                onClick={() => remove(f)}
                className="text-gray-400 hover:text-red-500"
                title="Delete"
              >
                🗑
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
