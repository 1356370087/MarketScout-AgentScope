import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { TeamBoard, type BoardTask } from "./team-board";

describe("team task contract", () => {
  it("shows separate titles, descriptions and remaining blockers using canonical fields", () => {
    const onTask = vi.fn();
    const base = { owner: null, blocks: [], blockedBy: [], unresolvedBlockedBy: [], admission_status: "accepted" };
    const tasks: BoardTask[] = [
      { ...base, task_id: "accepted", subject: "核实规模", description: "核对官方规模数据", status: "completed" },
      { ...base, task_id: "waiting", subject: "核实增速", description: "核对官方增长数据", status: "running" },
      { ...base, task_id: "summary", subject: "汇总研究", description: "完整说明与任务标题应分别展示", status: "pending", admission_status: "pending",
        blockedBy: ["accepted", "waiting"], unresolvedBlockedBy: ["waiting"] },
    ];
    render(<TeamBoard tasks={tasks} members={[]} onTask={onTask} activityTaskIds={["accepted", "waiting", "summary"]} />);
    const card = screen.getByRole("heading", { name: "汇总研究", level: 4 }).closest("article")!;
    fireEvent.click(within(card).getByText("任务说明"));
    expect(within(card).getByText("完整说明与任务标题应分别展示")).toBeVisible();
    fireEvent.click(within(card).getByText("任务依赖"));
    expect(within(card).getByText("仍在等待：核实增速")).toBeVisible();
    expect(within(card).queryByText("仍在等待：核实规模")).not.toBeInTheDocument();
    fireEvent.click(within(card).getByRole("button", { name: "核实规模" }));
    expect(onTask).toHaveBeenCalledWith("accepted");
  });
});
