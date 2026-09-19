import type { PublicEvent, RunSnapshot } from "../contracts/research";
import type { SourceSelection } from "../contracts/documents";
import type { PublicationTheme } from "../contracts/publications";
import { API_BASE, apiFetch, headers } from "./http";
import { fetchEventSource } from "@microsoft/fetch-event-source";
import { refreshBrowserSession } from "../auth";

export const researchApi = {
  listRuns: (status?: string) => apiFetch<{ items: Array<Record<string, unknown>>; next_cursor?: string }>(`/runs?limit=50${status ? `&status=${encodeURIComponent(status)}` : ""}`),
  getRun: (id: string) => apiFetch<RunSnapshot>(`/runs/${encodeURIComponent(id)}`),
  createRun: (query: string, configurable: Record<string, unknown>, title?: string, sourceSelection?: SourceSelection, publicationTheme?: PublicationTheme) => apiFetch<{ run_id: string }>("/runs", {
    method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({ title, messages: [{ role: "user", content: query }], configurable, source_selection: sourceSelection, ...(publicationTheme ? { publication_theme: publicationTheme } : {}) }),
  }),
  humanAction: (runId: string, actionId: string, action: string, message = "") => apiFetch(`/runs/${runId}/human-actions/${actionId}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, message }) }),
  feedback: (runId: string, payload: Record<string, unknown>) => apiFetch(`/runs/${runId}/feedback`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  cancel: (runId: string) => apiFetch(`/runs/${runId}/cancel`, { method: "POST" }),
  resume: (runId: string) => apiFetch(`/runs/${runId}/resume`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }),
};

export async function subscribeToRun(options: {
  runId: string; after: number; signal: AbortSignal;
  onOpen: () => void; onEvent: (event: PublicEvent) => void;
  onReconnect: () => void; onCursorAhead: () => Promise<void>;
}): Promise<void> {
  let authRetried = false;
  let retryAttempt = 0;
  await fetchEventSource(`${API_BASE}/runs/${options.runId}/events?after=${options.after}`, {
    method: "GET", signal: options.signal, openWhenHidden: true,
    headers: Object.fromEntries((await headers({ Accept: "text/event-stream", "Last-Event-ID": String(options.after) })).entries()),
    async onopen(response) {
      if (response.ok) { retryAttempt = 0; options.onOpen(); return; }
      if (response.status === 401 && !authRetried) {
        authRetried = true;
        if (!await refreshBrowserSession()) throw new Error("sse-auth-failed");
        throw new Error("sse-auth-refreshed");
      }
      if (response.status === 409) { await options.onCursorAhead(); throw new Error("cursor-ahead"); }
      throw new Error(`sse-${response.status}`);
    },
    onmessage(message) { if (message.data) options.onEvent(JSON.parse(message.data) as PublicEvent); },
    onclose() { throw new Error("sse-disconnected"); },
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
