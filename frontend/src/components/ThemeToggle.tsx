import { useTheme } from "../hooks/useTheme";

export function ThemeToggle() {
  const { dark, toggle } = useTheme();
  return (
    <button
      onClick={toggle}
      title="Toggle theme"
      className="inline-flex min-h-11 min-w-11 items-center justify-center rounded border border-gray-300 px-2 py-1 text-sm lg:min-h-0 lg:min-w-0 dark:border-gray-600"
    >
      {dark ? "☀️" : "🌙"}
    </button>
  );
}
