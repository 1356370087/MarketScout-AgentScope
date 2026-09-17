import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { researchApi } from "@/lib/api";
import { resolvedSecurityApprovalIds } from "@/lib/security-approvals";
import type { SecurityApproval } from "@/lib/types";
import { useResearchRunStore } from "@/stores/research-run-store";
import { ResearchWorkspace } from "@/features/research/research-workspace";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("@/hooks/use-run-stream", () => ({ useRunStream: vi.fn() }));
vi.mock("./app-shell", () => ({
  AppShell: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/features/research/task-activity-drawer", () => ({ TaskActivityDrawer: () => null }));
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
  beforeEach(() => {
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
