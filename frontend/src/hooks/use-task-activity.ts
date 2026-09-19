"use client";

import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState } from "react";
import { researchApi, subscribeToTaskActivity } from "@/lib/api";
import { emptyTaskActivityState, mergeTaskActivityHistory, reduceTaskActivity } from "@/lib/task-activity-reducer";
import type { TaskActivityTimelineState } from "@/lib/task-activity-reducer";
import type { TaskActivityEvent, TaskActivityKind, TaskActivityPage } from "@/lib/types";

type Connection = "connecting" | "connected" | "reconnecting" | "closed" | "error";
const terminalTypes = ["task.completed", "task.failed", "task.cancelled", "task.timed_out"];

export function useTaskActivity(runId: string, taskId: string, kind?: TaskActivityKind) {
  const activityKey = `${runId}:${taskId}:${kind ?? "all"}`;
  const history = useQuery({
    queryKey: ["task-activity", runId, taskId, kind ?? "all"],
    queryFn: () => researchApi.taskActivity(runId, taskId, { kind }),
    enabled: Boolean(runId && taskId),
    staleTime: 3_000,
  });
  const terminal = history.data?.items.some((event) => terminalTypes.includes(event.type)) ?? false;
  const [timelineEntry, setTimelineEntry] = useState<{ key: string; state: TaskActivityTimelineState }>(() => ({ key: activityKey, state: emptyTaskActivityState() }));
  const [connectionEntry, setConnectionEntry] = useState<{ key: string; value: Connection }>(() => ({ key: activityKey, value: "connecting" }));
  const [olderEntry, setOlderEntry] = useState<{ key: string; pages: TaskActivityPage[] }>(() => ({ key: activityKey, pages: [] }));
  const timeline = timelineEntry.key === activityKey ? timelineEntry.state : emptyTaskActivityState();
  const connection = connectionEntry.key === activityKey ? connectionEntry.value : "connecting";
  const older = useMemo(() => olderEntry.key === activityKey ? olderEntry.pages : [], [activityKey, olderEntry]);
  const setConnection = useCallback((value: Connection) => setConnectionEntry({ key: activityKey, value }), [activityKey]);
  const updateTimeline = useCallback((event: TaskActivityEvent) => setTimelineEntry((entry) => ({
    key: activityKey,
    state: reduceTaskActivity(entry.key === activityKey ? entry.state : emptyTaskActivityState(), event),
  })), [activityKey]);
  const mergeTimeline = useCallback((items: TaskActivityEvent[]) => setTimelineEntry((entry) => ({
    key: activityKey,
    state: mergeTaskActivityHistory(entry.key === activityKey ? entry.state : emptyTaskActivityState(), items),
  })), [activityKey]);

  useEffect(() => {
    if (!history.data || history.data.source !== "native" || terminal) return;
    const controller = new AbortController();
    let stopped = false;
    let authRestarts = 0;
    let cursor = history.data.last_event_id;
    async function connect() {
      try {
        await subscribeToTaskActivity({
          runId,
          taskId,
          after: cursor,
          signal: controller.signal,
          onOpen: () => setConnection("connected"),
          onReconnect: () => setConnection("reconnecting"),
          onCursorAhead: async () => {
            const page = await researchApi.taskActivity(runId, taskId, { kind });
            cursor = page.last_event_id;
            mergeTimeline(page.items);
          },
          onEvent: (event) => {
            cursor = Math.max(cursor, event.sequence);
            updateTimeline(event);
            if (terminalTypes.includes(event.type)) {
              setConnection("closed");
              controller.abort();
            }
          },
        });
        if (!stopped) setConnection("closed");
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        if (!stopped && message.includes("cursor-ahead")) { await connect(); return; }
        if (!stopped && message.includes("sse-auth-refreshed") && authRestarts < 1) {
          authRestarts += 1;
          await connect();
          return;
        }
        if (!stopped && !(error instanceof DOMException && error.name === "AbortError")) setConnection("error");
      }
    }
    void connect();
    return () => { stopped = true; controller.abort(); };
  }, [history.data, kind, mergeTimeline, runId, setConnection, taskId, terminal, updateTimeline]);

  const mergedTimeline = useMemo(() => {
    let state = mergeTaskActivityHistory(emptyTaskActivityState(), history.data?.items ?? []);
    for (const page of older) state = mergeTaskActivityHistory(state, page.items);
    return mergeTaskActivityHistory(state, timeline.events);
  }, [history.data?.items, older, timeline.events]);
  const hasMore = older.at(-1)?.has_more ?? history.data?.has_more ?? false;
  const loadOlder = useCallback(async () => {
    const oldest = older.at(-1)?.oldest_sequence ?? history.data?.oldest_sequence;
    if (!oldest || !hasMore) return;
    const page = await researchApi.taskActivity(runId, taskId, { before: oldest, kind });
    setOlderEntry((entry) => ({ key: activityKey, pages: [...(entry.key === activityKey ? entry.pages : []), page] }));
    mergeTimeline(page.items);
  }, [activityKey, hasMore, history.data?.oldest_sequence, kind, mergeTimeline, older, runId, taskId]);

  return useMemo(() => ({
    events: mergedTimeline.events,
    connection: history.data?.source === "native" && !terminal ? connection : "closed",
    loading: history.isLoading,
    error: history.error,
    source: history.data?.source ?? "summary_only",
    detailLevel: history.data?.detail_level ?? "summary",
    hasMore,
    loadOlder,
  }), [connection, hasMore, history.data, history.error, history.isLoading, loadOlder, mergedTimeline.events, terminal]);
}
