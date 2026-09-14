import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { researchApi } from "@/lib/api";
import { useResearchRunStore } from "@/stores/research-run-store";
import { HumanActionCard } from "./research-workspace";

describe("fetch budget HITL", () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it.each([
    ["增加预算并继续", "approve"],
    ["不增加，生成部分报告", "deny"],
    ["取消整项研究", "cancel"],
  ])("%s submits an explicit decision", async (label, decision) => {
    useResearchRunStore.getState().reset("run-budget");
    const submit = vi.spyOn(researchApi, "humanAction").mockResolvedValue({ status: "accepted" });
    render(<HumanActionCard runId="run-budget" action={{
      action_id: "budget-action", type: "fetch_budget_approval",
      payload: { content_markdown: "上限从 40 次增加至 80 次" },
      allowed_actions: ["approve", "deny", "cancel"],
    }} />);
    expect(screen.queryByText("提交修改")).not.toBeInTheDocument();
    expect(submit).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: label }));
    await waitFor(() => expect(submit).toHaveBeenCalledWith("run-budget", "budget-action", decision, ""));
  });
});
