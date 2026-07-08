import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";

// Per-session flag so the warning shows once per browser session (survives page refreshes)
// and is cleared on logout so the next fresh login prompts again.
const DISMISS_KEY = "multichat_pw_prompt_dismissed";

/**
 * Warns the user when they're signed in with the default seeded password. Shown once per
 * browser session (not on every refresh) and re-armed on a fresh sign-in, so it keeps
 * nudging until the password is changed. The user may continue without changing.
 */
export function DefaultPasswordPrompt() {
  const { user, loading } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [dismissed, setDismissed] = useState(
    () => sessionStorage.getItem(DISMISS_KEY) === "1"
  );

  // Re-arm on a real logout so the next fresh login prompts again. Guard on `loading` so
  // the brief null-user window while /me resolves on refresh doesn't clear the flag.
  useEffect(() => {
    if (!loading && !user) {
      sessionStorage.removeItem(DISMISS_KEY);
      setDismissed(false);
    }
  }, [user, loading]);

  function dismiss() {
    sessionStorage.setItem(DISMISS_KEY, "1");
    setDismissed(true);
  }

  if (!user?.is_default_password || dismissed) return null;
  if (location.pathname === "/settings/security") return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="w-full max-w-md rounded-xl border border-gray-200 bg-white p-6 shadow-xl dark:border-gray-700 dark:bg-gray-900">
        <h2 className="text-lg font-semibold">Secure your account</h2>
        <p className="mt-2 text-sm text-gray-600 dark:text-gray-300">
          You're signed in with the default seeded password. We recommend changing it now to
          keep your account safe.
        </p>
        <div className="mt-5 flex justify-end gap-2">
          <button
            onClick={dismiss}
            className="rounded-lg border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-100 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-800"
          >
            Continue without changing
          </button>
          <button
            onClick={() => {
              dismiss();
              navigate("/settings/security");
            }}
            className="rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white hover:brightness-110"
          >
            Change password
          </button>
        </div>
      </div>
    </div>
  );
}
