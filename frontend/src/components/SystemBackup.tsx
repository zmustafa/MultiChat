import { API_BASE, getToken } from "../api/client";

/** Full-system export/import (.zip). Lives under Settings → General. */
export function SystemBackup() {
  function exportEverything() {
    if (
      !confirm(
        "⚠️ Export sensitive data?\n\nThis backup contains SECRETS in plaintext — your API " +
          "keys, provider credentials, and OAuth tokens — along with all personas, snippets, " +
          "evals, integrations and sessions.\n\nStore the .zip somewhere safe and never share " +
          "it.\n\nContinue with the export?"
      )
    )
      return;
    const token = getToken();
    fetch(`${API_BASE}/api/system/export`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((r) => r.blob())
      .then((blob) => {
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
        a.download = `multichat-backup-${stamp}.zip`;
        a.click();
        URL.revokeObjectURL(url);
      })
      .catch((e) => alert((e as Error).message));
  }

  async function importEverything(file: File) {
    if (
      !confirm(
        "Restore backup?\n\nThis will REPLACE ALL of your current data — settings, " +
          "API keys, providers, personas, snippets, evals, integrations and every " +
          "session — with the contents of this backup. This cannot be undone.\n\n" +
          "Continue?"
      )
    )
      return;
    const form = new FormData();
    form.append("file", file);
    try {
      const res = await fetch(`${API_BASE}/api/system/import`, {
        method: "POST",
        headers: { Authorization: `Bearer ${getToken()}` },
        body: form,
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `Import failed (HTTP ${res.status})`);
      }
      alert("Backup restored. The app will now reload.");
      window.location.reload();
    } catch (e) {
      alert((e as Error).message);
    }
  }

  return (
    <div className="mx-auto max-w-5xl p-4">
      <section className="rounded-lg border border-gray-200 bg-white shadow-sm dark:border-gray-700 dark:bg-gray-900">
        <div className="border-b border-gray-200 px-5 py-4 dark:border-gray-700">
          <h2 className="font-medium">Full system backup</h2>
          <p className="mt-0.5 text-xs text-gray-500">
            Export or restore ALL of your data — settings, API keys, providers, personas,
            snippets, evals, integrations, and every session — as a single .zip. Importing
            REPLACES all current data and cannot be undone.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2 p-5">
          <button
            onClick={exportEverything}
            className="rounded-lg border border-gray-300 px-4 py-1.5 text-sm hover:bg-gray-100 dark:border-gray-600 dark:hover:bg-gray-800"
          >
            🗄 Export everything (.zip)
          </button>
          <label className="cursor-pointer rounded-lg border border-gray-300 px-4 py-1.5 text-sm hover:bg-gray-100 dark:border-gray-600 dark:hover:bg-gray-800">
            ♻ Import everything (.zip)
            <input
              type="file"
              accept=".zip,application/zip"
              hidden
              onChange={(e) => {
                if (e.target.files?.[0]) importEverything(e.target.files[0]);
                e.target.value = "";
              }}
            />
          </label>
        </div>
      </section>
    </div>
  );
}
