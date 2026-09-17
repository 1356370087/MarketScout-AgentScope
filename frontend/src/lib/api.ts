import { fetchEventSource } from "@microsoft/fetch-event-source";
import { csrfHeaders, refreshBrowserSession } from "./auth";
import type { CapabilitiesResponse, PublicEvent, RunSnapshot, TaskActivityEvent, TaskActivityKind, TaskActivityPage } from "./contracts/research";
import type { DocumentChunk, ResearchDocument, SourceSelection } from "./contracts/documents";
import type { EgressModeState, EgressRuntimeMode, EgressState, EgressTarget, SecurityApproval } from "./contracts/security";
import type { ModelCatalogResponse } from "./contracts/models";
import type { PublicationEvent, PublicationRequestFormat, PublicationJob, PublicationListResponse, PublicationTheme } from "./contracts/publications";
import type { RunUsageResponse, UsageAnalyticsResponse } from "./contracts/usage";

const API_BASE = process.env.NEXT_PUBLIC_RESEARCH_API_BASE ?? "/api/research";
const DOCUMENT_PAGE_SIZE = 100;

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

type DocumentListParams = { q?: string; status?: string; limit?: number; offset?: number };

async function headers(extra?: HeadersInit): Promise<Headers> {
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

const listDocumentsPage = (params: DocumentListParams = {}) => {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => { if (value !== undefined && value !== "") query.set(key, String(value)); });
  return apiFetch<{ items: ResearchDocument[]; total: number }>(`/documents${query.size ? `?${query}` : ""}`);
};

export async function listAllDocuments(params: Omit<DocumentListParams, "limit" | "offset"> = {}) {
  // The API caps one page at 100; walk the bounded result set for selectors
  // that need every ready document.
  const items: ResearchDocument[] = [];
  let offset = 0;
  let total = 0;
  while (true) {
    const page = await listDocumentsPage({ ...params, limit: DOCUMENT_PAGE_SIZE, offset });
    total = page.total;
    items.push(...page.items);
    if (!page.items.length || items.length >= total || page.items.length < DOCUMENT_PAGE_SIZE) break;
    offset += page.items.length;
  }
  return { items, total };
}

export const researchApi = {
  capabilities: () => apiFetch<CapabilitiesResponse>("/capabilities"),
  models: () => apiFetch<ModelCatalogResponse>("/models"),
  listRuns: (status?: string) => apiFetch<{ items: Array<Record<string, unknown>>; next_cursor?: string }>(`/runs?limit=50${status ? `&status=${encodeURIComponent(status)}` : ""}`),
  getRun: (id: string) => apiFetch<RunSnapshot>(`/runs/${encodeURIComponent(id)}`),
  runUsage: (id: string) => apiFetch<RunUsageResponse>(`/runs/${encodeURIComponent(id)}/usage`),
  usageAnalytics: (params: Record<string, string | number | undefined> = {}) => {
    const query = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => { if (value !== undefined && value !== "") query.set(key, String(value)); });
    return apiFetch<UsageAnalyticsResponse>(`/usage/analytics${query.size ? `?${query}` : ""}`);
  },
  createRun: (query: string, configurable: Record<string, unknown>, title?: string, sourceSelection?: SourceSelection, publicationTheme?: PublicationTheme) => apiFetch<{ run_id: string }>("/runs", {
    method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({ title, messages: [{ role: "user", content: query }], configurable, source_selection: sourceSelection, ...(publicationTheme ? { publication_theme: publicationTheme } : {}) }),
  }),
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
  listDocuments: listDocumentsPage,
  listAllDocuments: (params: Omit<DocumentListParams, "limit" | "offset"> = {}) => listAllDocuments(params),
  getDocument: (id: string) => apiFetch<ResearchDocument>(`/documents/${encodeURIComponent(id)}`),
  documentChunks: (id: string) => apiFetch<{ items: DocumentChunk[] }>(`/documents/${encodeURIComponent(id)}/chunks`),
  uploadDocument: async (file: File) => {
    const form = new FormData(); form.set("file", file);
    return apiFetch<{ document: ResearchDocument; deduplicated: boolean }>("/documents", { method: "POST", body: form });
  },
  retryDocument: (id: string) => apiFetch<ResearchDocument>(`/documents/${encodeURIComponent(id)}/retry`, { method: "POST" }),
  reindexDocument: (id: string) => apiFetch<ResearchDocument>(`/documents/${encodeURIComponent(id)}/reindex`, { method: "POST" }),
  deleteDocument: (id: string) => apiFetch<{ id: string; status: string }>(`/documents/${encodeURIComponent(id)}`, { method: "DELETE" }),
  humanAction: (runId: string, actionId: string, action: string, message = "") => apiFetch(`/runs/${runId}/human-actions/${actionId}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, message }) }),
  securityApprovals: (runId: string, signal?: AbortSignal) => apiFetch<{ run_id: string; version: number; approvals: SecurityApproval[] }>(`/runs/${encodeURIComponent(runId)}/security-approvals?status=pending`, { signal }),
  resolveSecurityApproval: (runId: string, approvalId: string, decision: "allow_once" | "allow_run" | "deny", reason = "") => apiFetch<SecurityApproval>(`/runs/${encodeURIComponent(runId)}/security-approvals/${encodeURIComponent(approvalId)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision, reason }) }),
  egressState: (runId: string, signal?: AbortSignal) => apiFetch<EgressState>(`/runs/${encodeURIComponent(runId)}/egress-state`, { signal }),
  decideEgressTarget: (runId: string, target: EgressTarget, decision: "allow_run" | "block_run" | "revoke", reason = "") => apiFetch<EgressTarget>(`/runs/${encodeURIComponent(runId)}/egress-targets/${target.target_id}/decision`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision, reason, expected_version: target.version }) }),
  egressMode: (runId: string) => apiFetch<EgressModeState>(`/runs/${encodeURIComponent(runId)}/egress-mode`),
  switchEgressMode: (runId: string, mode: EgressRuntimeMode) => apiFetch<EgressModeState>(`/runs/${encodeURIComponent(runId)}/egress-mode`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode }) }),
  feedback: (runId: string, payload: Record<string, unknown>) => apiFetch(`/runs/${runId}/feedback`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  cancel: (runId: string) => apiFetch(`/runs/${runId}/cancel`, { method: "POST" }),
  resume: (runId: string) => apiFetch(`/runs/${runId}/resume`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }),
  taskActivity: (runId: string, taskId: string, options: { before?: number; limit?: number; kind?: TaskActivityKind } = {}) => {
    const query = new URLSearchParams({ limit: String(options.limit ?? 100) });
    if (options.before !== undefined) query.set("before", String(options.before));
    if (options.kind) query.set("kind", options.kind);
    return apiFetch<TaskActivityPage>(`/runs/${encodeURIComponent(runId)}/tasks/${encodeURIComponent(taskId)}/activity?${query}`);
  },
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
    onerror(error) {
      const message = error instanceof Error ? error.message : String(error);
      if (message.includes("cursor-ahead") || message.includes("sse-auth")) throw error;
      options.onReconnect();
      const delay = Math.min(30_000, 1_000 * 2 ** retryAttempt);
      retryAttempt += 1;
      return delay;
    },
  });
}

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
      if (response.status === 401 && !authRetried) { authRetried = true; await refreshBrowserSession(); throw new Error("sse-auth-refreshed"); }
      if (response.status === 409) { await options.onCursorAhead(); throw new Error("cursor-ahead"); }
      throw new Error(`sse-${response.status}`);
    },
    onmessage(message) { if (message.data) options.onEvent(JSON.parse(message.data) as PublicEvent); },
    onerror(error) {
      const message = error instanceof Error ? error.message : String(error);
      if (message.includes("cursor-ahead") || message.includes("sse-auth") || message.includes("sse-401")) throw error;
      options.onReconnect();
      const delay = Math.min(30_000, 1_000 * 2 ** retryAttempt);
      retryAttempt += 1;
      return delay;
    },
  });
}

export async function subscribeToTaskActivity(options: {
  runId: string; taskId: string; after: number; signal: AbortSignal;
  onOpen: () => void; onEvent: (event: TaskActivityEvent) => void;
  onReconnect: () => void; onCursorAhead: () => Promise<void>;
}): Promise<void> {
  let authRetried = false;
  let retryAttempt = 0;
  const path = `/runs/${encodeURIComponent(options.runId)}/tasks/${encodeURIComponent(options.taskId)}/activity/stream?after=${options.after}`;
  await fetchEventSource(`${API_BASE}${path}`, {
    method: "GET", signal: options.signal, openWhenHidden: true,
    headers: Object.fromEntries((await headers({ Accept: "text/event-stream", "Last-Event-ID": String(options.after) })).entries()),
    async onopen(response) {
      if (response.ok) { retryAttempt = 0; options.onOpen(); return; }
      if (response.status === 401 && !authRetried) { authRetried = true; await refreshBrowserSession(); throw new Error("task-sse-auth-refreshed"); }
      if (response.status === 409) { await options.onCursorAhead(); throw new Error("task-cursor-ahead"); }
      throw new Error(`task-sse-${response.status}`);
    },
    onmessage(message) { if (message.data) options.onEvent(JSON.parse(message.data) as TaskActivityEvent); },
    onerror(error) {
      const message = error instanceof Error ? error.message : String(error);
      if (message.includes("cursor-ahead") || message.includes("sse-auth")) throw error;
      options.onReconnect();
      const delay = Math.min(30_000, 1_000 * 2 ** retryAttempt);
      retryAttempt += 1;
      return delay;
    },
  });
}
