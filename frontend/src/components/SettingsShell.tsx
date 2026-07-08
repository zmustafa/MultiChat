import { Link } from "react-router-dom";
import { ThemeToggle } from "./ThemeToggle";
import { useAuth } from "../auth/AuthContext";

/** Shared chrome (back link, title, theme toggle, sign-out) for the settings sub-pages. */
export function SettingsShell({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  const { logout, user } = useAuth();
  return (
    <div className="flex h-full flex-col bg-gray-50 dark:bg-gray-950">
      <header className="flex items-center justify-between border-b border-gray-200 bg-white px-4 py-2 dark:border-gray-700 dark:bg-gray-900">
        <div className="flex items-center gap-3">
          <Link to="/" className="text-sm text-blue-500">
            ← Chat
          </Link>
          <h1 className="text-lg font-semibold">{title}</h1>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-500">{user?.email}</span>
          <ThemeToggle />
          <button
            onClick={logout}
            className="rounded border border-gray-300 px-2 py-1 text-sm dark:border-gray-600"
          >
            Sign out
          </button>
        </div>
      </header>
      <div className="flex-1 overflow-y-auto">{children}</div>
    </div>
  );
}
