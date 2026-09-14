"use client";

import { Moon, Sun } from "lucide-react";
import { useSyncExternalStore } from "react";
import { applyInterfaceTheme, loadInterfaceTheme, THEME_EVENT, type InterfaceTheme } from "@/lib/theme";

function subscribeToTheme(callback: () => void) {
  window.addEventListener(THEME_EVENT, callback);
  return () => window.removeEventListener(THEME_EVENT, callback);
}

export function ThemeToggle({ compact = false }: { compact?: boolean }) {
  const theme = useSyncExternalStore<InterfaceTheme>(subscribeToTheme, loadInterfaceTheme, () => "light");

  const next = theme === "light" ? "dark" : "light";
  const label = next === "dark" ? "切换到深色模式" : "切换到浅色模式";
  return <button
    type="button"
    className={`theme-toggle ${compact ? "compact" : ""}`}
    aria-label={label}
    title={label}
    onClick={() => applyInterfaceTheme(next)}
  >
    <span className="theme-toggle-track" aria-hidden="true"><Sun size={13} /><Moon size={13} /><i data-theme={theme} /></span>
    {!compact && <span>{theme === "light" ? "浅色界面" : "深色界面"}</span>}
  </button>;
}
