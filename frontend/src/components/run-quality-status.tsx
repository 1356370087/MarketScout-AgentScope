import { AlertTriangle } from "lucide-react";
import type { ResearchRunState } from "@/lib/types";

export function RunQualityStatus({ state }: { state: ResearchRunState }) {
  if (!state.terminal) return null;
  const partial = state.resultStatus === "partial";
  const degraded = state.qualityGate?.status === "degraded";
  if (!partial && !degraded) return null;
  const failed = state.status === "failed" || state.resultStatus === "failed";
  const hasReport = Boolean(state.report.trim());
  const reasons = state.qualityGate?.reason_codes;
  const handoffRejected = Array.isArray(reasons) && reasons.includes("handoff_rejected");
  const budgetExhausted = state.terminationReason?.startsWith("max_turns");

  return <section className="panel" role="status" aria-label="运行质量状态">
    <div className="panel-header">
      <h2><AlertTriangle size={16} /> {failed ? "研究失败" : hasReport ? (partial ? "部分完成" : "降级交付") : "未生成报告"}</h2>
      {degraded && <span className="status-chip" data-status="degraded">质量门禁降级</span>}
    </div>
    <div className="panel-body">
      <p>{hasReport
        ? (failed ? "本次运行失败，已有报告内容仅供排查参考。" : "报告已生成，仍有证据或覆盖缺口，请结合报告中的限制说明使用。")
        : "本次运行未生成报告，研究结果仍有证据或覆盖缺口。"}</p>
      {handoffRejected && <p>部分研究结果未通过交接门禁，相关需求尚未得到充分证据支持。</p>}
      {budgetExhausted && <p>{failed || !hasReport ? "研究轮次已用尽。" : "研究轮次已用尽，本次运行以部分结果结束。"}</p>}
    </div>
  </section>;
}
