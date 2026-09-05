import { useEffect, useState } from "react";

const THEME_EVENT = "multichat:theme";

function storedTheme(): boolean {
  return localStorage.getItem("multichat_theme") === "dark";
}

function applyTheme(dark: boolean) {
  document.documentElement.classList.toggle("dark", dark);
  localStorage.setItem("multichat_theme", dark ? "dark" : "light");
}

export function useTheme() {
  const [dark, setDark] = useState(storedTheme);

  useEffect(() => {
    applyTheme(dark);
    const sync = (event: Event) => {
      setDark((event as CustomEvent<boolean>).detail);
    };
    const syncStorage = () => setDark(storedTheme());
    window.addEventListener(THEME_EVENT, sync);
    window.addEventListener("storage", syncStorage);
    return () => {
      window.removeEventListener(THEME_EVENT, sync);
      window.removeEventListener("storage", syncStorage);
    };
    // Every hook instance starts from the same persisted value; subsequent changes flow
    // through THEME_EVENT rather than each instance writing independently.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const toggle = () => {
    const next = !document.documentElement.classList.contains("dark");
    applyTheme(next);
    setDark(next);
    window.dispatchEvent(new CustomEvent<boolean>(THEME_EVENT, { detail: next }));
  };

  return { dark, toggle };
}
