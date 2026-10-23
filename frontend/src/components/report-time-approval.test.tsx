import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { HumanActionCard } from "@/features/research/approval-center";
import { researchApi } from "@/lib/api";
import type { PendingHumanAction, PublicEvent } from "@/lib/types";
import { emptyRunState, hydrateSnapshot, reducePublicEvent } from "@/lib/run-reducer";
import { resolvedHumanActionIds } from "@/lib/security-approvals";
import { useResearchRunStore } from "@/stores/research-run-store";

const request: PendingHumanAction = {
  action_id: "time-1", type: "report_time_extension_approval",
  payload: { version: 1, content_markdown: "报告已保存，等待延期授权。", remaining_seconds: 120,
    estimated_report_seconds: 700, suggested_extension_seconds: 600, extension_count: 0, max_requests: 2 },
  allowed_actions: ["approve", "deny", "cancel"],
};

beforeEach(() => {
  vi.restoreAllMocks();
  resolvedHumanActionIds.clear();
  useResearchRunStore.getState().reset("report-run");
  useResearchRunStore.setState({ pendingHumanAction: request });
});
afterEach(cleanup);

it("does not approve on render and submits the selected whole minutes and exact version", async () => {
  const send = vi.spyOn(researchApi, "humanAction").mockResolvedValue({});
  render(<HumanActionCard runId="report-run" action={request} />);
  expect(send).not.toHaveBeenCalled();
  expect(screen.getByText(/已暂停执行和计时/)).toBeVisible();
  fireEvent.change(screen.getByRole("spinbutton", { name: "延长分钟数" }), { target: { value: "5" } });
  expect(screen.getByText(/低于建议的 10 分钟/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "延长并继续" }));
  await waitFor(() => expect(send).toHaveBeenCalledOnce());
  expect(send).toHaveBeenCalledWith("report-run", "time-1", "approve", "", {
    source_selection: undefined, expected_version: 1, extension_seconds: 300,
  });
  expect(useResearchRunStore.getState().pendingHumanAction).toBeUndefined();
});

it("rejects invalid minutes and preserves the pending request on a stale decision", async () => {
  vi.spyOn(researchApi, "humanAction").mockRejectedValue(new Error("审批已更新"));
  render(<HumanActionCard runId="report-run" action={request} />);
  fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "2" } });
  expect(screen.getByRole("button", { name: "延长并继续" })).toBeDisabled();
  fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "15" } });
  fireEvent.click(screen.getByRole("button", { name: "延长并继续" }));
  await screen.findByRole("alert");
  expect(useResearchRunStore.getState().pendingHumanAction).toEqual(request);
});

it.each([
  ["deny", "不延长，交付当前可用结果"],
  ["cancel", "取消整项研究"],
])("sends %s without an extension amount", async (action, label) => {
  const send = vi.spyOn(researchApi, "humanAction").mockResolvedValue({});
  render(<HumanActionCard runId="report-run" action={request} />);
  fireEvent.click(screen.getByRole("button", { name: label }));
  await waitFor(() => expect(send).toHaveBeenCalledOnce());
  expect(send.mock.calls[0][4]).toEqual({ source_selection: undefined, expected_version: 1 });
});

it("preserves time proposal fields across SSE and a page refresh", () => {
  const event: PublicEvent = { schema_version: 2, event_id: "report-time-1", timestamp: "2026-10-23T00:00:00Z",
    run_id: "report-run", sequence: 1, type: "approval.required", payload: {
    action_id: request.action_id, approval_type: "report_time_extension",
    ...request.payload, allowed_actions: request.allowed_actions,
  }};
  const live = reducePublicEvent(emptyRunState("report-run"), event);
  expect(live.status).toBe("awaiting_report_time_extension_approval");
  expect(live.pendingHumanAction?.payload.suggested_extension_seconds).toBe(600);
  const restored = hydrateSnapshot(emptyRunState("report-run"), {
    run_id: "report-run", status: "awaiting_report_time_extension_approval",
    pending_human_action: live.pendingHumanAction, last_event_id: 1,
  });
  expect(restored.pendingHumanAction).toEqual(live.pendingHumanAction);
});
