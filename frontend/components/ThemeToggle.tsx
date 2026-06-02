"use client";

import { useEffect, useState } from "react";

type Theme = "system" | "light" | "dark";

// Apply a theme to <html>: add/remove the `dark` class. "system" follows the OS.
function applyTheme(theme: Theme) {
  const dark =
    theme === "dark" ||
    (theme === "system" &&
      window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("dark", dark);
}

const OPTIONS: { key: Theme; label: string }[] = [
  { key: "system", label: "System" },
  { key: "light", label: "Light" },
  { key: "dark", label: "Dark" },
];

// System / Light / Dark switcher. The actual class is set pre-paint by an inline
// script in the root layout (no flash); this just lets the user change + persist
// the choice, and keeps "system" live as the OS preference changes.
export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>("system");

  useEffect(() => {
    const saved = localStorage.getItem("theme") as Theme | null;
    if (saved === "light" || saved === "dark" || saved === "system") {
      setTheme(saved);
    }
  }, []);

  useEffect(() => {
    if (theme !== "system") return;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => applyTheme("system");
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [theme]);

  function choose(next: Theme) {
    setTheme(next);
    localStorage.setItem("theme", next);
    applyTheme(next);
  }

  return (
    <div className="flex items-center gap-0.5 rounded-md border border-border p-0.5 text-xs">
      {OPTIONS.map((o) => (
        <button
          key={o.key}
          type="button"
          onClick={() => choose(o.key)}
          aria-pressed={theme === o.key}
          className={
            "rounded px-2 py-0.5 transition " +
            (theme === o.key
              ? "bg-secondary text-secondary-foreground"
              : "text-muted-foreground hover:text-foreground")
          }
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}
