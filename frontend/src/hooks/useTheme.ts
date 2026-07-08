import { useEffect, useState } from "react";

export function useTheme() {
  const [dark, setDark] = useState(
    () => localStorage.getItem("multichat_theme") === "dark"
  );

  useEffect(() => {
    const root = document.documentElement;
    if (dark) root.classList.add("dark");
    else root.classList.remove("dark");
    localStorage.setItem("multichat_theme", dark ? "dark" : "light");
  }, [dark]);

  return { dark, toggle: () => setDark((d) => !d) };
}
