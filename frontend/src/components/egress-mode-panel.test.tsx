import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { researchApi } from "@/lib/api";
import { useResearchRunStore } from "@/stores/research-run-store";
import { ApprovalCenter } from "./approval-center";

const state = {
  baseline_mode: "auto", effective_mode: "auto", can_resolve: true,
  allowed_modes: ["manual", "auto"] as ("manual" | "auto" | "open")[],
  health: { remaining_calls: 180, calls_used: 20 }, targets: [], records: [],
};

describe("ApprovalCenter", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    useResearchRunStore.getState().reset("run-egress");
    vi.spyOn(researchApi, "egressState").mockResolvedValue(state);
  });
  afterEach(cleanup);

  it("does not open itself when a request arrives", () => {
    const change = vi.fn();
    render(<ApprovalCenter runId="run-egress" open={false} onOpenChange={change} />);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(change).not.toHaveBeenCalled();
  });

  it("shows an empty queue and disables modes beyond the baseline", async () => {
    render(<ApprovalCenter runId="run-egress" open onOpenChange={vi.fn()} />);
    expect(screen.getByText("暂时没有待处理请求")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "域名权限" }));
    await screen.findByText("剩余 180 次");
    expect(screen.getByRole("button", { name: "全部放行" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "逐项人工" })).toBeEnabled();
  });

  it("switches modes using the authoritative snapshot", async () => {
    const switchSpy = vi.spyOn(researchApi, "switchEgressMode").mockResolvedValue({ ...state, effective_mode: "manual" });
    render(<ApprovalCenter runId="run-egress" open onOpenChange={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: "域名权限" }));
    await screen.findByText("剩余 180 次");
    vi.spyOn(researchApi, "egressState").mockResolvedValue({ ...state, effective_mode: "manual" });
    fireEvent.click(screen.getByRole("button", { name: "逐项人工" }));
    await waitFor(() => expect(switchSpy).toHaveBeenCalledWith("run-egress", "manual"));
    await screen.findByText("当前：逐项人工");
  });

  it("shows degradation and a read-only permission view", async () => {
    vi.spyOn(researchApi, "egressState").mockResolvedValue({ ...state, can_resolve: false,
      health: { remaining_calls: 0, degraded: true } });
    render(<ApprovalCenter runId="run-egress" open onOpenChange={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: "域名权限" }));
    await screen.findByText("自动分类已降级，转为人工确认");
    expect(screen.getByRole("button", { name: "逐项人工" })).toBeDisabled();
  });

  it("closes without submitting a decision", async () => {
    const change = vi.fn();
    const resolve = vi.spyOn(researchApi, "resolveSecurityApproval");
    render(<ApprovalCenter runId="run-egress" open onOpenChange={change} />);
    fireEvent.click(screen.getByRole("button", { name: "关闭审批中心" }));
    expect(change).toHaveBeenCalledWith(false);
    expect(resolve).not.toHaveBeenCalled();
  });
});
