import type { ResearchSource, TaskActivityEvent } from "@/lib/types";

export type ActivityGroup = { id: string; type: "tool" | "sources" | "event"; events: TaskActivityEvent[] };

/** Pair actual tool receipts; adjacent sources only share a task/iteration group. */
export function groupActivity(events: TaskActivityEvent[]): ActivityGroup[] {
  const groups: ActivityGroup[] = [];
  const pending = new Map<string, ActivityGroup>();
  for (const event of events) {
    const callId = event.payload.tool_call_id;
    if (event.kind === "tool" && typeof callId === "string" && callId) {
      const key = `${event.task_id}:${event.iteration ?? ""}:${callId}`;
      const started = pending.get(key);
      if (started && event.type !== "tool.started") {
        started.events.push(event);
        if (event.type === "tool.completed" || event.type === "tool.failed") pending.delete(key);
      } else {
        const group: ActivityGroup = { id: event.event_id, type: "tool", events: [event] };
        groups.push(group);
        if (event.type === "tool.started") pending.set(key, group);
      }
    } else if (event.kind === "source") {
      const last = groups.at(-1);
      if (last?.type === "sources" && last.events[0].task_id === event.task_id && last.events[0].iteration === event.iteration) last.events.push(event);
      else groups.push({ id: event.event_id, type: "sources", events: [event] });
    } else groups.push({ id: event.event_id, type: "event", events: [event] });
  }
  return groups;
}

export function sourceHref(value: unknown): string | undefined {
  if (typeof value !== "string") return;
  if (/^\/documents\/[a-zA-Z0-9-]+(?:\?chunk=[a-zA-Z0-9-]+)?$/.test(value)) return value;
  try { const url = new URL(value); return ["https:", "http:"].includes(url.protocol) ? url.href : undefined; } catch { return; }
}

export function sourceKey(source: ResearchSource): string { return source.source_id || source.url; }

export function sourceDomain(source: ResearchSource): string {
  if (source.source_type === "local_document" || source.url.startsWith("/documents/")) return "企业资料库";
  if (source.domain) return source.domain;
  try { return new URL(source.url).hostname; } catch { return "研究来源"; }
}

export function matchSource(href: string | undefined, sources: ResearchSource[]): ResearchSource | undefined {
  const target = sourceHref(href);
  if (!target) return;
  // Paths, query parameters and fragments remain significant for evidence identity.
  return sources.find((source) => sourceHref(source.url) === target);
}

export function activitySource(event: TaskActivityEvent): ResearchSource | undefined {
  const href = sourceHref(event.payload.url);
  if (!href) return;
  return { source_id: String(event.payload.source_id ?? href), task_id: event.task_id, url: href,
    title: typeof event.payload.title === "string" ? event.payload.title : event.title,
    domain: typeof event.payload.domain === "string" ? event.payload.domain : undefined };
}

export const activityPhaseNames: Record<string, string> = {
  queued: "排队", initializing: "准备", reasoning: "模型规划", tool_execution: "工具执行", evidence_review: "证据评估",
  quality_check: "质量复核", gap_recovery: "补证恢复", compressing: "压缩", handoff: "交接", terminal: "已结束",
};
