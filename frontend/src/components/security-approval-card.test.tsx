import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SecurityApprovalCard } from "@/features/research/research-workspace";
import { researchApi } from "@/lib/api";
import { resolvedSecurityApprovalIds } from "@/lib/security-approvals";
import type { SecurityApproval } from "@/lib/types";
import { useResearchRunStore } from "@/stores/research-run-store";

const approval: SecurityApproval = {
  approval_id: "approval-1",
  run_id: "run-1",
  task_id: "task-1",
  fence_token: 1,
  kind: "network",
  capability: "tool.egress",
  target: { domain: "sqlite.org", port: 443 },
  target_fingerprint: "fingerprint-1",
  status: "pending",
  requested_at: 1,
  expires_at: 9999999999,
};

describe("SecurityApprovalCard", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    resolvedSecurityApprovalIds.clear();
    useResearchRunStore.getState().reset("run-1");
    useResearchRunStore.getState().setSecurityApprovals([approval]);
  });

  afterEach(cleanup);

  it("does not submit an approval already resolved by SSE", () => {
    const resolve = vi.spyOn(researchApi, "resolveSecurityApproval").mockResolvedValue(approval);
    render(<SecurityApprovalCard runId="run-1" approval={approval} />);
    useResearchRunStore.getState().setSecurityApprovals([]);

    fireEvent.click(screen.getByRole("button", { name: /本次研究允许/ }));

    expect(resolve).not.toHaveBeenCalled();
  });

  it("keeps an approval visible until the authoritative response", async () => {
    let finish: ((value: SecurityApproval) => void) | undefined;
    vi.spyOn(researchApi, "resolveSecurityApproval").mockImplementation(
      () => new Promise((resolve) => { finish = resolve; }),
    );
    vi.spyOn(researchApi, "securityApprovals").mockResolvedValue({
      run_id: "run-1",
      version: 2,
      approvals: [],
    });
    render(<SecurityApprovalCard runId="run-1" approval={approval} />);

    fireEvent.click(screen.getByRole("button", { name: /本次研究允许/ }));

    expect(useResearchRunStore.getState().pendingSecurityApprovals).toEqual([approval]);
    expect(screen.getByRole("button", { name: /允许此次访问/ })).toBeDisabled();
    finish?.(approval);
    await waitFor(() => expect(researchApi.securityApprovals).toHaveBeenCalledTimes(1));
  });

  it("keeps other concurrently pending approvals visible for allow-run", async () => {
    const matchingApproval = {
      ...approval,
      approval_id: "approval-2",
      task_id: "task-2",
    };
    useResearchRunStore.getState().setSecurityApprovals([approval, matchingApproval]);
    let finish: ((value: SecurityApproval) => void) | undefined;
    const resolve = vi.spyOn(researchApi, "resolveSecurityApproval").mockImplementation(
      () => new Promise((complete) => { finish = complete; }),
    );
    vi.spyOn(researchApi, "securityApprovals").mockResolvedValue({
      run_id: "run-1",
      version: 3,
      approvals: [],
    });
    render(<SecurityApprovalCard runId="run-1" approval={approval} />);

    fireEvent.click(screen.getByRole("button", { name: /本次研究允许/ }));

    expect(useResearchRunStore.getState().pendingSecurityApprovals).toEqual([approval, matchingApproval]);
    expect(resolve).toHaveBeenCalledTimes(1);
    finish?.(approval);
    await waitFor(() => expect(researchApi.securityApprovals).toHaveBeenCalledTimes(1));
  });

  it("does not restore a resolved approval when reconciliation fails", async () => {
    vi.spyOn(researchApi, "resolveSecurityApproval").mockResolvedValue(approval);
    vi.spyOn(researchApi, "securityApprovals").mockRejectedValue(
      new Error("temporary reconciliation failure"),
    );
    render(<SecurityApprovalCard runId="run-1" approval={approval} />);

    fireEvent.click(screen.getByRole("button", { name: /允许此次访问/ }));

    await waitFor(() => {
      expect(researchApi.resolveSecurityApproval).toHaveBeenCalledTimes(1);
    });
    expect(useResearchRunStore.getState().pendingSecurityApprovals).toEqual([]);
  });

  it("restores a genuinely pending approval and permits a retry", async () => {
    const resolve = vi.spyOn(researchApi, "resolveSecurityApproval").mockRejectedValue(
      new Error("temporary mutation failure"),
    );
    vi.spyOn(researchApi, "securityApprovals").mockResolvedValue({
      run_id: "run-1",
      version: 1,
      approvals: [approval],
    });
    render(<SecurityApprovalCard runId="run-1" approval={approval} />);

    fireEvent.click(screen.getByRole("button", { name: /允许此次访问/ }));
    await screen.findByRole("alert");
    expect(useResearchRunStore.getState().pendingSecurityApprovals).toEqual([approval]);

    fireEvent.click(screen.getByRole("button", { name: /允许此次访问/ }));
    await waitFor(() => expect(resolve).toHaveBeenCalledTimes(2));
  });

  it("does not turn arbitrary HTTP 400 errors into successful approval", async () => {
    vi.spyOn(researchApi, "resolveSecurityApproval").mockRejectedValue(new Error("400:invalid request"));
    vi.spyOn(researchApi, "securityApprovals").mockResolvedValue({ run_id: "run-1", version: 1, approvals: [approval] });
    render(<SecurityApprovalCard runId="run-1" approval={approval} />);
    fireEvent.click(screen.getByRole("button", { name: /允许此次访问/ }));
    await screen.findByRole("alert");
    expect(useResearchRunStore.getState().pendingSecurityApprovals).toEqual([approval]);
    expect(resolvedSecurityApprovalIds.has("approval-1")).toBe(false);
  });

  it("a resolved approval is not restored by a lagging poll or SSE replay", async () => {
    vi.spyOn(researchApi, "resolveSecurityApproval").mockResolvedValue(approval);
    // The lagging reconciliation still reports the approval as pending.
    vi.spyOn(researchApi, "securityApprovals").mockResolvedValue({
      run_id: "run-1",
      version: 1,
      approvals: [approval],
    });
    render(<SecurityApprovalCard runId="run-1" approval={approval} />);

    fireEvent.click(screen.getByRole("button", { name: /允许此次访问/ }));
    await waitFor(() => expect(researchApi.securityApprovals).toHaveBeenCalledTimes(1));

    expect(useResearchRunStore.getState().pendingSecurityApprovals).toEqual([]);

    // An SSE replay of security.approval.required must not re-add the card.
    const { emptyRunState, reducePublicEvent } = await import("@/lib/run-reducer");
    const next = reducePublicEvent(emptyRunState("run-1"), {
      type: "security.approval.required",
      payload: { approval_id: "approval-1", kind: "network" },
    } as never);
    expect(next.pendingSecurityApprovals).toEqual([]);
  });
});
