import { useState } from "react";
import { apiFetch } from "../api/client";
import { useAuth } from "../auth/AuthContext";

/** Change-password form. Used on the Security settings page and the first-login prompt. */
export function ChangePasswordForm({
  onSuccess,
  compact = false,
}: {
  onSuccess?: () => void;
  compact?: boolean;
}) {
  const { refreshUser } = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (next.length < 6) {
      setError("New password must be at least 6 characters.");
      return;
    }
    if (next !== confirm) {
      setError("New password and confirmation don't match.");
      return;
    }
    setBusy(true);
    try {
      await apiFetch("/api/auth/change-password", {
        method: "POST",
        body: JSON.stringify({ current_password: current, new_password: next }),
      });
      setDone(true);
      setCurrent("");
      setNext("");
      setConfirm("");
      await refreshUser();
      onSuccess?.();
    } catch (err) {
      setError((err as Error).message || "Could not change password.");
    } finally {
      setBusy(false);
    }
  }

  const inputClass =
    "w-full rounded-lg border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800";

  return (
    <form onSubmit={submit} className={compact ? "space-y-3" : "max-w-sm space-y-3"}>
      <div>
        <label className="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-300">
          Current password
        </label>
        <input
          type="password"
          autoComplete="current-password"
          value={current}
          onChange={(e) => setCurrent(e.target.value)}
          required
          className={inputClass}
        />
      </div>
      <div>
        <label className="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-300">
          New password
        </label>
        <input
          type="password"
          autoComplete="new-password"
          value={next}
          onChange={(e) => setNext(e.target.value)}
          required
          className={inputClass}
        />
      </div>
      <div>
        <label className="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-300">
          Confirm new password
        </label>
        <input
          type="password"
          autoComplete="new-password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          required
          className={inputClass}
        />
      </div>

      {error && <p className="text-xs text-red-600 dark:text-red-400">{error}</p>}
      {done && (
        <p className="text-xs text-green-600 dark:text-green-400">
          Password changed successfully.
        </p>
      )}

      <button
        type="submit"
        disabled={busy || !current || !next || !confirm}
        className="rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white hover:brightness-110 disabled:opacity-50"
      >
        {busy ? "Changing…" : "Change password"}
      </button>
    </form>
  );
}
