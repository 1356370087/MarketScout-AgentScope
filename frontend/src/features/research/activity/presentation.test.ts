import { describe, expect, it } from "vitest";
import type { TaskActivityEvent } from "@/lib/types";
import { groupActivity, matchSource, sourceHref } from "./presentation";

const event = (sequence: number, type: string, call = "a", overrides: Partial<TaskActivityEvent> = {}): TaskActivityEvent => ({
  schema_version: 1, event_id: `e-${sequence}`, sequence, run_id: "r", task_id: "t", iteration: 1,
  timestamp: "2026-10-03T00:00:00Z", type, kind: "tool", phase: "tool_execution", status: "running",
  title: type, summary: "", payload: { tool_call_id: call }, ...overrides,
});

describe("activity presentation", () => {
  it("pairs interleaved calls by their real identities", () => {
    const groups = groupActivity([event(1, "tool.started", "a"), event(2, "tool.started", "b"), event(3, "tool.completed", "b"), event(4, "tool.completed", "a")]);
    expect(groups.map((group) => group.events.map((item) => item.sequence))).toEqual([[1, 4], [2, 3]]);
  });
  it("keeps tasks and iterations separate even when call IDs repeat", () => {
    const groups = groupActivity([event(1, "tool.started"), event(2, "tool.completed", "a", { iteration: 2 }), event(3, "tool.completed", "a", { task_id: "other" })]);
    expect(groups).toHaveLength(3);
  });
  it("preserves missing starts and failed attempts before a retry", () => {
    const groups = groupActivity([event(1, "tool.completed"), event(2, "tool.started"), event(3, "tool.failed"), event(4, "tool.started"), event(5, "tool.completed")]);
    expect(groups.map((group) => group.events.map((item) => item.sequence))).toEqual([[1], [2, 3], [4, 5]]);
  });
  it("does not imply that adjacent sources belong to a tool call", () => {
    const groups = groupActivity([event(1, "tool.started"), event(2, "source.discovered", "a", { kind: "source" }), event(3, "source.discovered", "b", { kind: "source" }), event(4, "tool.completed")]);
    expect(groups.map((group) => [group.type, group.events.length])).toEqual([["tool", 2], ["sources", 2]]);
  });
});

describe("source identity", () => {
  const sources = [{ source_id: "s", url: "https://EXAMPLE.com/Report?id=A#part" }];
  it("normalizes the host but preserves path, query and fragment", () => {
    expect(matchSource("https://example.com/Report?id=A#part", sources)?.source_id).toBe("s");
    for (const url of ["https://example.com/report?id=A#part", "https://example.com/Report?id=a#part", "https://example.com/Report?id=A"]) expect(matchSource(url, sources)).toBeUndefined();
  });
  it("supports local document locations and rejects unsafe links", () => {
    expect(sourceHref("/documents/doc-1?chunk=chunk-1")).toBe("/documents/doc-1?chunk=chunk-1");
    for (const url of ["javascript:alert(1)", "data:text/html,hello", "//unknown.example/path", "/api/auth/logout"]) expect(sourceHref(url)).toBeUndefined();
  });
});
