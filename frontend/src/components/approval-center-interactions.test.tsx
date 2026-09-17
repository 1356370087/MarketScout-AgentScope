import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { researchApi } from "@/lib/api";
import { emptyRunState, hydrateSnapshot, reducePublicEvent } from "@/lib/run-reducer";
import { resolvedHumanActionIds } from "@/lib/security-approvals";
import type { PendingHumanAction, PublicEvent, SecurityApproval } from "@/lib/types";
import { useResearchRunStore } from "@/stores/research-run-store";
import { ApprovalCenter, HumanActionCard, SecurityApprovalCard } from "@/features/research/approval-center";

beforeEach(() => {
  vi.restoreAllMocks();
  resolvedHumanActionIds.clear();
  useResearchRunStore.getState().reset("run-actions");
});
afterEach(cleanup);

it.each([
  ["clarification", "answer", "回答并继续"],
  ["plan_approval", "approve", "批准并继续"],
  ["outline_approval", "revise", "提交修改"],
  ["fetch_budget_approval", "deny", "不增加，生成部分报告"],
] as const)("handles %s using its allowed actions", async (type, action, label) => {
  const pending: PendingHumanAction = { action_id: type, type,
    payload: { content_markdown: "## 研究范围\n\n请核对 **范围**。<script>alert(1)</script>" },
    allowed_actions: [action] };
  useResearchRunStore.setState({ pendingHumanAction: pending });
  const submit = vi.spyOn(researchApi, "humanAction").mockResolvedValue({});
  const { container } = render(<HumanActionCard runId="run-actions" action={pending} />);
  expect(container.querySelector("script")).toBeNull();
  expect(screen.getByRole("heading", { name: "研究范围" })).toBeVisible();
  expect(screen.queryByRole("button", { name: "取消整项研究" })).toBeNull();
  const textbox = screen.queryByRole("textbox");
  if (textbox) fireEvent.change(textbox, { target: { value: "补充范围" } });
  fireEvent.click(screen.getByRole("button", { name: label }));
  await waitFor(() => expect(submit).toHaveBeenCalledOnce());
  expect(useResearchRunStore.getState().pendingHumanAction).toBeUndefined();
  const restored = hydrateSnapshot(emptyRunState("run-actions"), {
    run_id: "run-actions", status: "running", pending_human_action: pending, last_event_id: 0,
  });
  expect(restored.pendingHumanAction).toBeUndefined();
});

it.each(["network", "command", "filesystem", "tool_effect", "mcp_oauth"] as const)("renders %s without expanded raw parameters", (kind) => {
  const approval: SecurityApproval = { approval_id: "sec-kind", run_id: "run-actions", task_id: "task",
    fence_token: 1, kind, capability: "test", target: { domain: "long.example", port: 443,
      command: "python build.py", path: "/workspace/out", tool: "build", url: "https://auth.example/oauth" },
    target_fingerprint: "fp", status: "pending", requested_at: 1, expires_at: 9999999999 };
  const { container } = render(<SecurityApprovalCard runId="run-actions" approval={approval} disabled />);
  expect(container.querySelector("details[open]")).toBeNull();
  for (const button of screen.getAllByRole("button")) expect(button).toBeDisabled();
  expect(screen.queryAllByRole("link")).toHaveLength(kind === "mcp_oauth" ? 1 : 0);
});

it("does not apply a late human response to another run", async () => {
  let finish!: (value: Record<string, unknown>) => void;
  vi.spyOn(researchApi, "humanAction").mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  const action: PendingHumanAction = { action_id: "old", type: "plan_approval", payload: {}, allowed_actions: ["approve"] };
  render(<HumanActionCard runId="run-actions" action={action} />);
  fireEvent.click(screen.getByRole("button", { name: "批准并继续" }));
  useResearchRunStore.getState().reset("run-new");
  const current = { ...action, action_id: "new" };
  useResearchRunStore.setState({ pendingHumanAction: current });
  await act(async () => finish({}));
  expect(useResearchRunStore.getState().pendingHumanAction).toEqual(current);
});

it("navigates tabs with arrows and honours independent human interaction permission", async () => {
  vi.spyOn(researchApi, "egressState").mockResolvedValue({ baseline_mode: "auto", effective_mode: "auto",
    can_resolve: true, can_interact: false, allowed_modes: ["manual", "auto"], targets: [], records: [], health: {} });
  useResearchRunStore.setState({ pendingHumanAction: { action_id: "plan", type: "plan_approval", payload: {}, allowed_actions: ["approve"] } });
  render(<ApprovalCenter runId="run-actions" open onOpenChange={vi.fn()} />);
  await waitFor(() => expect(screen.getByRole("button", { name: "批准并继续" })).toBeDisabled());
  const pending = screen.getByRole("tab", { name: /待处理/ });
  pending.focus();
  fireEvent.keyDown(pending, { key: "ArrowRight" });
  expect(screen.getByRole("tab", { name: "域名权限" })).toHaveFocus();
  await screen.findByText("自动分类可用");
});

it("rejects older classifier and mode versions even with newer event sequence", () => {
  const event = (sequence: number, type: string, payload: object) => ({ run_id: "run-actions", sequence, type, payload } as PublicEvent);
  let state = reducePublicEvent(emptyRunState("run-actions"), event(1, "security.egress_classified", {
    fingerprint: "exact", domain: "one.github.io", capability: "tool.egress", port: 443, version: 3, verdict: "deny",
  }));
  state = reducePublicEvent(state, event(2, "security.egress_classified", {
    fingerprint: "exact", domain: "one.github.io", capability: "tool.egress", port: 443, version: 2, verdict: "allow",
  }));
  expect(state.egressClassifications[0].verdict).toBe("deny");
  state = reducePublicEvent(state, event(3, "security.egress_mode_changed", { mode: "auto", effective_mode: "manual", version: 5 }));
  state = reducePublicEvent(state, event(4, "security.egress_mode_changed", { mode: "open", effective_mode: "open", version: 4 }));
  expect(state.egressMode?.effective_mode).toBe("manual");
});
