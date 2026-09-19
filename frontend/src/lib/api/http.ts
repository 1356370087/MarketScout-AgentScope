import { csrfHeaders, refreshBrowserSession } from "../auth";

export const API_BASE = process.env.NEXT_PUBLIC_RESEARCH_API_BASE ?? "/api/research";

export async function headers(extra?: HeadersInit): Promise<Headers> {
  return csrfHeaders(extra);
}

export async function apiFetch<T>(path: string, init: RequestInit = {}, refresh = false): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    credentials: "same-origin",
    headers: await headers(init.headers),
  });
  if (response.status === 401 && !refresh && await refreshBrowserSession()) return apiFetch<T>(path, init, true);
  if (response.status === 401 && typeof window !== "undefined") {
    window.location.replace("/login");
  }
  if (!response.ok) throw new Error(`${response.status}:${await response.text()}`);
  return response.json() as Promise<T>;
}
