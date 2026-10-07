import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ResearchEfficiencyPanel } from "./research-efficiency-panel";

describe("research progress admission", () => {
  it("distinguishes finished execution from admitted handoffs and partial conclusions", () => {
    render(<ResearchEfficiencyPanel partial reason="research_no_progress" progress={{
      document_count: 3, processed_chunks: 8, total_chunks: 12,
      candidate_count: 30, admitted_count: 12, counters: { extraction_cache_hits: 2 },
      tasks: { first: { status: "completed", admission_status: "rejected" }, second: { status: "completed", admission_status: "accepted_with_caveats" } },
      requirements: { requirement: { text: "备份的必要条件", status: "partial", supplement_rounds: 2, stagnant_rounds: 2, gaps: [{ kind: "factual", reason: "所选页面没有直接说明。" }] } },
    }} />);
    expect(screen.getByText("部分研究已完成")).toBeInTheDocument();
    expect(screen.getByText("研究员执行结束 2 项 · 交接通过 1 项")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("未证实内容不会作为结论");
    expect(screen.getByText(/已检查 8 \/ 12/)).toBeInTheDocument();
    expect(screen.getByText(/提取复用 2 次/)).toBeInTheDocument();
  });

  it("does not label an empty partial report as an admitted conclusion", () => {
    render(<ResearchEfficiencyPanel partial reason="supplement_round_limit" />);
    expect(screen.getByText("资料不足，尚无已准入结论")).toBeInTheDocument();
  });
});
