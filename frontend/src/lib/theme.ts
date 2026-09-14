export const THEME_STORAGE_KEY = "odr.interface-theme.v1";
export const THEME_EVENT = "odr:theme-change";

export type InterfaceTheme = "light" | "dark";

export function normalizeTheme(value: unknown): InterfaceTheme {
  return value === "dark" ? "dark" : "light";
}

export function loadInterfaceTheme(): InterfaceTheme {
  if (typeof window === "undefined") return "light";
  try {
    return normalizeTheme(window.localStorage.getItem(THEME_STORAGE_KEY));
  } catch {
    return "light";
  }
}

export function applyInterfaceTheme(theme: InterfaceTheme, persist = true) {
  if (typeof document === "undefined") return;
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
  if (persist) {
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, theme);
    } catch {
      // Storage can be unavailable in private or policy-restricted contexts.
    }
  }
  window.dispatchEvent(new CustomEvent<InterfaceTheme>(THEME_EVENT, { detail: theme }));
}
