import { afterEach, describe, expect, it, vi } from "vitest";
import { applyInterfaceTheme, loadInterfaceTheme, normalizeTheme, THEME_EVENT, THEME_STORAGE_KEY } from "./theme";

describe("interface theme", () => {
  afterEach(() => {
    localStorage.clear();
    document.documentElement.dataset.theme = "light";
    document.documentElement.style.colorScheme = "";
  });

  it("defaults unknown and missing values to the light theme", () => {
    expect(normalizeTheme(undefined)).toBe("light");
    expect(normalizeTheme("system")).toBe("light");
    expect(loadInterfaceTheme()).toBe("light");
  });

  it("persists and broadcasts a dark theme change", () => {
    const listener = vi.fn();
    window.addEventListener(THEME_EVENT, listener);

    applyInterfaceTheme("dark");

    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(document.documentElement.style.colorScheme).toBe("dark");
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
    expect(listener).toHaveBeenCalledOnce();
    window.removeEventListener(THEME_EVENT, listener);
  });
});
