import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { TaskActivityEvent } from "@/lib/types";
import { ActivityCard } from "./activity-cards";
import { groupActivity } from "./presentation";

describe("web search progress", () => {
  it("shows independent providers, query counts and partial failure details", () => {
    const base: TaskActivityEvent = { schema_version: 1, event_id: "e1", sequence: 1, run_id: "r", task_id: "t",
      timestamp: "2026-10-04T00:00:00Z", type: "tool.progress", kind: "tool", phase: "tool_execution",
      status: "running", title: "收到搜索结果", summary: "Python 文档", payload: { tool_call_id: "call", provider: "bing", result_count: 5 } };
    const failure: TaskActivityEvent = { ...base, event_id: "e2", sequence: 2, status: "warning", title: "搜索渠道暂不可用", summary: "rate_limited", payload: { tool_call_id: "call", provider: "brave", error_code: "rate_limited" } };
    render(<ActivityCard group={groupActivity([base, failure])[0]} />);
    expect(screen.getByRole("list", { name: "搜索与抓取进度" })).toBeVisible();
    expect(screen.getByText("bing · 收到搜索结果")).toBeVisible();
    expect(screen.getByText("5 个候选来源")).toBeVisible();
    expect(screen.getByText("brave · 搜索渠道暂不可用")).toBeVisible();
  });
});
