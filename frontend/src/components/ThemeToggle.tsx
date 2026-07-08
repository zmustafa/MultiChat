import { useTheme } from "../hooks/useTheme";

export function ThemeToggle() {
  const { dark, toggle } = useTheme();
  return (
    <button
      onClick={toggle}
      title="Toggle theme"
      className="rounded border border-gray-300 px-2 py-1 text-sm dark:border-gray-600"
    >
      {dark ? "☀️" : "🌙"}
    </button>
  );
}
