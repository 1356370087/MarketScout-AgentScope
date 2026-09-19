"use client";

import { useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { researchApi, subscribeToPublications } from "@/lib/api";
import {
  emptyPublicationState,
  publicationEventNeedsRefetch,
  reducePublicationEvent,
} from "@/lib/publication-reducer";
import type { PublicationJob, PublicationListResponse } from "@/lib/types";

export function usePublications(runId: string, initial: PublicationJob[] = []) {
  const queryClient = useQueryClient();
  const itemsRef = useRef<PublicationJob[]>(initial);
  const cursorRef = useRef(0);
  const projectionRef = useRef(emptyPublicationState(initial));
  const query = useQuery({
    queryKey: ["publications", runId],
    queryFn: () => researchApi.publications(runId),
    placeholderData: initial.length ? { run_id: runId, items: initial, events_url: `/runs/${runId}/publications/events`, worker: "degraded" } satisfies PublicationListResponse : undefined,
  });
  const queryItems = query.data?.items;
  useEffect(() => {
    if (!queryItems) return;
    itemsRef.current = queryItems;
    const lastEventId = projectionRef.current.lastEventId;
    projectionRef.current = {
      ...emptyPublicationState(queryItems),
      lastEventId,
    };
  }, [queryItems]);
  useEffect(() => {
    cursorRef.current = 0;
    projectionRef.current = emptyPublicationState(itemsRef.current);
  }, [runId]);
  const pending = query.data?.items.some(
    (item) => item.status === "queued" || item.status === "running",
  ) ?? false;

  useEffect(() => {
    if (!pending) return;
    const controller = new AbortController();
    let stopped = false;
    let authRestarts = 0;
    const queryKey = ["publications", runId] as const;
    async function connect() {
      while (!stopped) {
        try {
          await subscribeToPublications({
            runId, after: cursorRef.current, signal: controller.signal,
            onOpen: () => undefined,
            onEvent: (event) => {
              cursorRef.current = Math.max(cursorRef.current, event.sequence);
              const needsRefetch = publicationEventNeedsRefetch(projectionRef.current, event);
              projectionRef.current = reducePublicationEvent(projectionRef.current, event);
              queryClient.setQueryData<PublicationListResponse>(queryKey, (current) => {
                if (!current) return current;
                return {
                  ...current,
                  items: current.items.map((item) => projectionRef.current.jobsById[item.publication_id] ?? item),
                };
              });
              if (needsRefetch) {
                void queryClient.invalidateQueries({ queryKey });
              }
            },
            onReconnect: () => undefined,
            onCursorAhead: async () => {
              cursorRef.current = 0;
              projectionRef.current = emptyPublicationState(itemsRef.current);
              await queryClient.invalidateQueries({ queryKey });
            },
          });
        } catch (error) {
          if (stopped || error instanceof DOMException && error.name === "AbortError") return;
          const message = error instanceof Error ? error.message : String(error);
          if (message === "publication-sse-auth-failed" || /sse-(401|403|404)$/.test(message)) return;
          if (message === "publication-sse-auth-refreshed" && authRestarts++ >= 1) return;
        }
        if (!stopped) await new Promise((resolve) => window.setTimeout(resolve, 1_000));
      }
    }
    void connect();
    return () => { stopped = true; controller.abort(); };
  }, [pending, queryClient, runId]);

  return query;
}
