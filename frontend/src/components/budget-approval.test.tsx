import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HumanActionCard } from "@/features/research/approval-center";
import { researchApi } from "@/lib/api";
import type { PendingHumanAction, PublicEvent } from "@/lib/types";
import { emptyRunState, hydrateSnapshot, reducePublicEvent } from "@/lib/run-reducer";
import { resolvedHumanActionIds } from "@/lib/security-approvals";
import { useResearchRunStore } from "@/stores/research-run-store";

const request: PendingHumanAction = { action_id: "budget-1", type: "budget_approval",
  payload: { dimension: "cost_micro_usd", content_markdown: "运行费用预算不足。" }, allowed_actions: ["approve", "cancel"] };

beforeEach(() => {
  vi.restoreAllMocks(); resolvedHumanActionIds.clear();
  useResearchRunStore.getState().reset("budget-run");
  useResearchRunStore.setState({ pendingHumanAction: request });
});
afterEach(cleanup);

it("requires an explicit amount and submits dollars as an exact total micro-USD limit", async () => {
  const send = vi.spyOn(researchApi, "humanAction").mockResolvedValue({});
  render(<HumanActionCard runId="budget-run" action={request} />);
  expect(screen.getByText(/模型费用（美元）/)).toBeVisible();
  const approve = screen.getByRole("button", { name: "确认新上限并继续" });
  expect(approve).toBeDisabled(); expect(send).not.toHaveBeenCalled();
  fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "7.25" } });
  fireEvent.click(approve);
  await waitFor(() => expect(send).toHaveBeenCalledOnce());
  expect(send.mock.calls[0][4]?.limits).toEqual({ cost_micro_usd: 7_250_000 });
});

it("cancel does not submit a budget change", async () => {
  const send = vi.spyOn(researchApi, "humanAction").mockResolvedValue({});
  render(<HumanActionCard runId="budget-run" action={request} />);
  fireEvent.click(screen.getByRole("button", { name: "取消整项研究" }));
  await waitFor(() => expect(send).toHaveBeenCalledOnce());
  expect(send.mock.calls[0][4]?.limits).toBeUndefined();
});

it("retains the requested dimension across SSE and reload", () => {
  const event: PublicEvent = { schema_version: 2, event_id: "budget-event", timestamp: "2026-11-18T00:00:00Z",
    run_id: "budget-run", sequence: 1, type: "approval.required", payload: {
      action_id: request.action_id, approval_type: "budget", ...request.payload, allowed_actions: request.allowed_actions,
    } };
  const live = reducePublicEvent(emptyRunState("budget-run"), event);
  expect(live.status).toBe("awaiting_budget_approval");
  expect(live.pendingHumanAction?.payload.dimension).toBe("cost_micro_usd");
  expect(hydrateSnapshot(emptyRunState("budget-run"), { run_id: "budget-run", status: live.status,
    pending_human_action: live.pendingHumanAction, last_event_id: 1 }).pendingHumanAction).toEqual(live.pendingHumanAction);
});

it("rechecks an account quota without changing the run ceiling", async () => {
  const send = vi.spyOn(researchApi, "humanAction").mockResolvedValue({});
  const action: PendingHumanAction = { ...request, payload: { ...request.payload, budget_scope: "account_month", available: 260000, required: 400000 } };
  const cache = new QueryClient();
  cache.setQueryData(["identity"], { permissions: ["platform.usage.read", "platform.quotas.write"] });
  render(<QueryClientProvider client={cache}><HumanActionCard runId="budget-run" action={action} /></QueryClientProvider>);
  expect(screen.getByRole("link", { name: "管理账户月度配额" })).toHaveAttribute("href", "/admin/usage");
  expect(screen.getByText(/0.400000 美元/)).toHaveTextContent("0.260000 美元");
  expect(screen.queryByRole("spinbutton")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "重新检查账户配额" }));
  await waitFor(() => expect(send).toHaveBeenCalledOnce());
  expect(send.mock.calls[0][4]?.limits).toBeUndefined();
});
