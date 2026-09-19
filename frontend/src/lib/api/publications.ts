import type { PublicationEvent, PublicationRequestFormat, PublicationJob, PublicationListResponse, PublicationTheme } from "../contracts/publications";
import { API_BASE, apiFetch, headers } from "./http";
import { fetchEventSource } from "@microsoft/fetch-event-source";
import { refreshBrowserSession } from "../auth";

function fallbackPublicationDownloadUrl(runId: string, publicationId: string): string {
  const base = API_BASE.replace(/\/+$/, "");
  return `${base}/runs/${encodeURIComponent(runId)}/publications/${encodeURIComponent(publicationId)}/download`;
}

/**
 * Resolve the server-provided download endpoint while keeping the BFF prefix.
 * The API returns a run-relative path (for example, `/runs/.../download`),
 * whereas the browser reaches that endpoint through `/api/research`.
 */
function resolvePublicationDownloadUrl(
  serverUrl: string | null | undefined,
  fallback: string,
): string {
  const candidate = typeof serverUrl === "string" ? serverUrl.trim() : "";
  if (!candidate) return fallback;

  if (/^https?:\/\//i.test(candidate)) {
    if (typeof window === "undefined") return fallback;
    try {
      const parsed = new URL(candidate);
      return parsed.origin === window.location.origin ? parsed.toString() : fallback;
    } catch {
      return fallback;
    }
  }

  const base = API_BASE.replace(/\/+$/, "");
  const path = candidate.startsWith("/") ? candidate : `/${candidate}`;
  return path === base || path.startsWith(`${base}/`) ? path : `${base}${path}`;
}

export const publicationsApi = {
  publications: (runId: string) => apiFetch<PublicationListResponse>(`/runs/${encodeURIComponent(runId)}/publications`),
  publicationStatus: (runId: string, publicationId: string) => apiFetch<PublicationJob>(`/runs/${encodeURIComponent(runId)}/publications/${encodeURIComponent(publicationId)}`),
  createPublication: (runId: string, format: PublicationRequestFormat, theme?: PublicationTheme) => apiFetch<PublicationJob>(`/runs/${encodeURIComponent(runId)}/publications`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ format, theme }),
  }),
  retryPublication: (runId: string, publicationId: string) => apiFetch<PublicationJob>(`/runs/${encodeURIComponent(runId)}/publications/${encodeURIComponent(publicationId)}/retry`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }),
  publicationDownloadUrl: (runId: string, publicationId: string, serverUrl?: string | null) =>
    resolvePublicationDownloadUrl(
      serverUrl,
      fallbackPublicationDownloadUrl(runId, publicationId),
    ),
};

export async function subscribeToPublications(options: {
  runId: string; after: number; signal: AbortSignal;
  onOpen: () => void; onEvent: (event: PublicationEvent) => void;
  onReconnect: () => void; onCursorAhead: () => Promise<void>;
}): Promise<void> {
  let authRetried = false;
  let retryAttempt = 0;
  const path = `/runs/${encodeURIComponent(options.runId)}/publications/events?after=${options.after}`;
  await fetchEventSource(`${API_BASE}${path}`, {
    method: "GET", signal: options.signal, openWhenHidden: true,
    headers: Object.fromEntries((await headers({ Accept: "text/event-stream", "Last-Event-ID": String(options.after) })).entries()),
    async onopen(response) {
      if (response.ok) { retryAttempt = 0; options.onOpen(); return; }
      if (response.status === 401 && !authRetried) {
        authRetried = true;
        if (!await refreshBrowserSession()) throw new Error("publication-sse-auth-failed");
        throw new Error("publication-sse-auth-refreshed");
      }
      if (response.status === 409) { await options.onCursorAhead(); throw new Error("publication-cursor-ahead"); }
      throw new Error(`publication-sse-${response.status}`);
    },
    onmessage(message) { if (message.data) options.onEvent(JSON.parse(message.data) as PublicationEvent); },
    onclose() { throw new Error("publication-sse-disconnected"); },
    onerror(error) {
      const message = error instanceof Error ? error.message : String(error);
      if (message.includes("cursor-ahead") || message.includes("sse-auth") || /sse-(401|403|404)$/.test(message)) throw error;
      options.onReconnect();
      const delay = Math.min(30_000, 1_000 * 2 ** retryAttempt);
      retryAttempt += 1;
      return delay;
    },
  });
}
