import type { TaskActivityEvent, TaskActivityKind, TaskActivityPage } from "../contracts/research";
import { API_BASE, apiFetch, headers } from "./http";
import { fetchEventSource } from "@microsoft/fetch-event-source";
import { refreshBrowserSession } from "../auth";

export const activityApi = {
  taskActivity: (runId: string, taskId: string, options: { before?: number; limit?: number; kind?: TaskActivityKind } = {}) => {
    const query = new URLSearchParams({ limit: String(options.limit ?? 100) });
    if (options.before !== undefined) query.set("before", String(options.before));
    if (options.kind) query.set("kind", options.kind);
    return apiFetch<TaskActivityPage>(`/runs/${encodeURIComponent(runId)}/tasks/${encodeURIComponent(taskId)}/activity?${query}`);
  },
};

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
      if (response.status === 401 && !authRetried) {
        authRetried = true;
        if (!await refreshBrowserSession()) throw new Error("task-sse-auth-failed");
        throw new Error("task-sse-auth-refreshed");
      }
      if (response.status === 409) { await options.onCursorAhead(); throw new Error("task-cursor-ahead"); }
      throw new Error(`task-sse-${response.status}`);
    },
    onmessage(message) { if (message.data) options.onEvent(JSON.parse(message.data) as TaskActivityEvent); },
    onclose() { throw new Error("task-sse-disconnected"); },
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
