import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { researchApi } from "@/lib/api";
import { resolvedSecurityApprovalIds } from "@/lib/security-approvals";
import type { SecurityApproval } from "@/lib/types";
import { useResearchRunStore } from "@/stores/research-run-store";
import { ResearchWorkspace } from "@/features/research/research-workspace";

const navigation = vi.hoisted(() => ({ search: "", push: vi.fn() }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: navigation.push }),
  useSearchParams: () => new URLSearchParams(navigation.search),
}));

vi.mock("@/hooks/use-run-stream", () => ({ useRunStream: vi.fn() }));
vi.mock("@/hooks/use-run-usage", () => ({ useRunUsage: () => ({ data: { task_operations: { "task-1": { model_call_count: 7, tool_call_count: 3 } } } }) }));
vi.mock("./app-shell", () => ({
  AppShell: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/features/research/task-activity-drawer", () => ({ TaskActivityDrawer: ({ task }: { task?: { model_call_count?: number; tool_call_count?: number } }) => task ? <output aria-label="详情调用统计">{task.model_call_count} / {task.tool_call_count}</output> : null }));
vi.mock("./token-usage-dashboard", () => ({
  TokenUsageDashboard: () => null,
  UsageCompactSummary: () => null,
}));

const approval: SecurityApproval = {
  approval_id: "approval-hydrated",
  run_id: "run-hydrated",
  task_id: "task-1",
  fence_token: 1,
  kind: "network",
  capability: "tool.egress",
  target: { domain: "example.com", port: 443 },
  target_fingerprint: "fingerprint-hydrated",
  status: "pending",
  requested_at: 1,
  expires_at: 9999999999,
};

describe("ResearchWorkspace security approval reconciliation", () => {
  it("renders durable native task counts", () => {
    useResearchRunStore.setState({ tasksById: { "task-1": { task_id: "task-1", title: "Test", status: "running" } } });
    navigation.search = "task=task-1";
    render(<ResearchWorkspace runId="run-hydrated" />);
    expect(screen.getByLabelText("详情调用统计")).toHaveTextContent("7 / 3");
  });
  beforeEach(() => {
    navigation.search = "";
    navigation.push.mockClear();
    vi.useFakeTimers();
    vi.restoreAllMocks();
    resolvedSecurityApprovalIds.clear();
    useResearchRunStore.getState().reset("run-hydrated");
    useResearchRunStore.getState().hydrate({
      run_id: "run-hydrated",
      status: "running",
      progress: {},
    } as never);
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it("keeps recorded tasks accessible after refreshing during report writing", () => {
    useResearchRunStore.getState().hydrate({ run_id: "run-hydrated", status: "running", last_event_id: 3, progress: { current_stage: "writing", task_items: { "task-1": { task_id: "task-1", title: "产品能力核验", status: "completed" } } } });
    render(<ResearchWorkspace runId="run-hydrated" />);
    expect(screen.getByRole("button", { name: /产品能力核验/ })).toBeInTheDocument();
    expect(screen.getByText("已记录任务")).toBeInTheDocument();
  });

  it("preserves task and unrelated URL parameters when changing views", () => {
    navigation.search = "task=task-1&from=history";
    render(<ResearchWorkspace runId="run-hydrated" />);
    fireEvent.click(screen.getByRole("tab", { name: "研究团队" }));
    expect(navigation.push).toHaveBeenCalledWith("/research/run-hydrated?task=task-1&from=history&view=team", { scroll: false });
  });

  it("keeps direction feedback on failure and clears it only after acceptance", async () => {
    const send = vi.spyOn(researchApi, "feedback").mockRejectedValueOnce(new Error("暂时不可用")).mockResolvedValueOnce({ status: "accepted" } as never);
    render(<ResearchWorkspace runId="run-hydrated" />);
    fireEvent.change(screen.getByLabelText("补充研究方向"), { target: { value: "优先核验官方来源" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "发送补充" })); });
    expect(screen.getByRole("alert")).toHaveTextContent("暂时不可用");
    expect(screen.getByLabelText("补充研究方向")).toHaveValue("优先核验官方来源");
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "发送补充" })); });
    expect(send).toHaveBeenLastCalledWith("run-hydrated", { type: "direction", message: "优先核验官方来源" });
    expect(screen.getByLabelText("补充研究方向")).toHaveValue("");
    expect(screen.getByText(/补充已受理/)).toBeInTheDocument();
  });

  it("shows partial and degraded quality even when report review is absent", () => {
    useResearchRunStore.getState().hydrate({
      run_id: "run-hydrated", status: "completed", last_event_id: 287,
      output: {
        markdown: "# Partial report", status: "partial",
        termination_reason: "max_turns_drained",
        quality_gate: { status: "degraded", reason_codes: ["handoff_rejected"] },
      },
    });
    render(<ResearchWorkspace runId="run-hydrated" />);
    expect(screen.getByText("部分完成")).toBeInTheDocument();
    expect(screen.getByText("质量门禁降级")).toBeInTheDocument();
    expect(screen.getByText(/部分研究结果未通过交接门禁/)).toBeInTheDocument();
  });

  it("does not announce a report for a failed run with degraded quality", () => {
    useResearchRunStore.getState().hydrate({
      run_id: "run-hydrated", status: "failed", last_event_id: 225,
      output: {
        markdown: "", status: "failed", termination_reason: "max_turns_drained",
        quality_gate: { status: "degraded", reason_codes: ["handoff_rejected"] },
      },
    });
    render(<ResearchWorkspace runId="run-hydrated" />);
    expect(screen.queryByText(/报告已生成/)).not.toBeInTheDocument();
    expect(screen.queryByText(/本次运行以部分结果结束/)).not.toBeInTheDocument();
    expect(screen.getByText(/未生成报告/)).toBeInTheDocument();
  });

  it("shows execution failure even before a quality result exists", () => {
    useResearchRunStore.getState().hydrate({
      run_id: "run-hydrated", status: "failed", last_event_id: 20,
      output: { markdown: "" },
    });
    render(<ResearchWorkspace runId="run-hydrated" />);
    expect(screen.getByText("研究失败")).toBeInTheDocument();
    expect(screen.getByText(/研究执行失败，未生成报告/)).toBeInTheDocument();
  });

  it("recovers a pending approval after the first hydrate poll fails", async () => {
    const list = vi.spyOn(researchApi, "securityApprovals")
      .mockRejectedValueOnce(new Error("temporary failure"))
      .mockResolvedValue({
        run_id: "run-hydrated",
        version: 2,
        approvals: [approval],
      });

    render(<ResearchWorkspace runId="run-hydrated" />);
    await act(async () => { await Promise.resolve(); });
    expect(list).toHaveBeenCalledTimes(1);

    await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });

    expect(list).toHaveBeenCalledTimes(2);
    expect(screen.getByText(/1 项请求等待你的决定/)).toBeInTheDocument();
  });
});
