import type { PublicEvent, RunSnapshot } from "../contracts/research";
import type { SourceSelection } from "../contracts/documents";
import type { PublicationTheme } from "../contracts/publications";
import { API_BASE, apiFetch, headers } from "./http";
import { fetchEventSource } from "@microsoft/fetch-event-source";
import { refreshBrowserSession } from "../auth";

type DraftReviewInput = { base_sha256: string; urls: string[]; requirement_ids: string[]; max_cost_micro_usd: number };
type ReviewVersions = {
  can_create: boolean;
  parent: { parent_run_id: string; base_sha256: string } | null;
  children: { run_id: string; status: string; completion_status?: string | null; review_passed?: boolean }[];
  drafts: string[];
  draft_attempts: Record<string, number>;
  delivered_sha256: string | null;
  requirements: { id: string; text: string; subject?: string | null }[];
  execution: { tasks: Record<string, string | null>; physical_fetches: number | null; cache_reuses: number | null;
    unknown: string[]; first_web_sequence: number | null; skills: { key: string; sequence: number }[];
    documents: { url: string; cached: boolean; read_at: string | null; published_at: string | null }[] };
};

export const researchApi = {
  readDraft: (id: string, sha: string) => apiFetch<{ markdown: string; sha256: string }>(`/runs/${encodeURIComponent(id)}/review-drafts/${encodeURIComponent(sha)}`),
  reviewVersions: (id: string) => apiFetch<ReviewVersions>(`/runs/${encodeURIComponent(id)}/review-versions`),
  previewDraftReview: (id: string, input: DraftReviewInput) => apiFetch<{ estimated_review_micro_usd: number | null; monthly_available: number | null }>(
    `/runs/${encodeURIComponent(id)}/draft-reviews/preview`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) }),
  createDraftReview: (id: string, input: DraftReviewInput, key: string) => apiFetch<{ run_id: string }>(
    `/runs/${encodeURIComponent(id)}/draft-reviews`, { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": key }, body: JSON.stringify(input) }),
  listRuns: (status?: string) => apiFetch<{ items: Array<Record<string, unknown>>; next_cursor?: string }>(`/runs?limit=50${status ? `&status=${encodeURIComponent(status)}` : ""}`),
  getRun: (id: string) => apiFetch<RunSnapshot>(`/runs/${encodeURIComponent(id)}`),
  createRun: (query: string, configurable: Record<string, unknown>, title?: string, sourceSelection?: SourceSelection, publicationTheme?: PublicationTheme, configuration?: { profile_revision_id: string; expected_platform_revision: string }) => apiFetch<{ run_id: string }>("/runs", {
    method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({ title, messages: [{ role: "user", content: query }], configurable, source_selection: sourceSelection, ...(publicationTheme ? { publication_theme: publicationTheme } : {}), ...configuration }),
  }),
  humanAction: (runId: string, actionId: string, action: string, message = "", options: { source_selection?: SourceSelection; expected_version?: number; extension_seconds?: number; limits?: Record<string, number> } = {}) => apiFetch(`/runs/${runId}/human-actions/${actionId}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, message, ...options }) }),
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
