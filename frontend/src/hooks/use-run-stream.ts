"use client";

import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { researchApi, subscribeToRun } from "@/lib/api";
import { useResearchRunStore } from "@/stores/research-run-store";

export function useRunStream(runId: string) {
  const queryClient = useQueryClient();

  useEffect(() => {
    const controller = new AbortController();
    let stopped = false;
    let authRestarts = 0;
    let usageRefreshTimer: ReturnType<typeof setTimeout> | undefined;
    const store = useResearchRunStore.getState();
    store.reset(runId);
    const active = () => !stopped && useResearchRunStore.getState().runId === runId;
    async function connect() {
      try {
        const snapshot = await researchApi.getRun(runId);
        if (!active()) return;
        useResearchRunStore.getState().hydrate(snapshot);
        if (["completed", "failed", "cancelled"].includes(snapshot.status)) return;
        useResearchRunStore.getState().setConnection("connecting");
        await subscribeToRun({
          runId, after: snapshot.last_event_id ?? 0, signal: controller.signal,
          onOpen: () => { if (active() && !controller.signal.aborted) useResearchRunStore.getState().setConnection("connected"); },
          onEvent: async (event) => {
            if (!active() || controller.signal.aborted) return;
            useResearchRunStore.getState().applyEvent(event);
            if (event.type === "run.usage.updated") {
              if (usageRefreshTimer) clearTimeout(usageRefreshTimer);
              usageRefreshTimer = setTimeout(() => {
                void queryClient.invalidateQueries({ queryKey: ["run-usage", runId] });
              }, 300);
            }
            if (["run.completed", "run.failed", "run.cancelled"].includes(event.type)) {
              // EOF can arrive before the final GET finishes. Stop transport
              // immediately so a completed run cannot re-enter reconnecting.
              controller.abort();
              useResearchRunStore.getState().setConnection("closed");
              try {
                const finalSnapshot = await researchApi.getRun(runId);
                if (!active()) return;
                useResearchRunStore.getState().hydrate(finalSnapshot);
                await queryClient.invalidateQueries({ queryKey: ["runs"] });
                await queryClient.invalidateQueries({ queryKey: ["run-usage", runId] });
              } catch {
                // The persisted terminal event remains authoritative; opening
                // the run again will reload its snapshot. Do not reject the
                // async event callback or restart a finished stream.
              }
            }
          },
          onReconnect: () => { if (active() && !controller.signal.aborted) useResearchRunStore.getState().setConnection("reconnecting", true); },
          onCursorAhead: async () => {
            const snapshot = await researchApi.getRun(runId);
            if (active()) useResearchRunStore.getState().hydrate(snapshot);
          },
        });
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        if (!stopped && message.includes("cursor-ahead")) { await connect(); return; }
        if (!stopped && message.includes("sse-auth-refreshed") && authRestarts < 1) {
          authRestarts += 1;
          await connect();
          return;
        }
        if (active() && !controller.signal.aborted && !(error instanceof DOMException && error.name === "AbortError")) {
          useResearchRunStore.getState().setConnection("error");
        }
      }
    }
    void connect();
    return () => { stopped = true; if (usageRefreshTimer) clearTimeout(usageRefreshTimer); controller.abort(); };
  }, [queryClient, runId]);
}
