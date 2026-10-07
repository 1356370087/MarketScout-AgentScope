import type { ResearchEfficiency } from "@/lib/contracts/research";

const reasons: Record<string, string> = {
  selected_sources_exhausted: "所选资料已检查完毕，部分问题仍无法证实。",
  research_no_progress: "连续补证没有带来新的有效信息，已停止重复工作。",
  supplement_round_limit: "补证已达到设定上限，保留已有证据与未解决问题。",
  report_budget_reserved: "已停止新增研究，将剩余预算用于交付结果。",
  report_time_reserved: "已停止新增研究，将剩余时间用于交付结果。",
  iteration_limit: "研究已达到轮次上限，未完成的要求会明确列出。",
};

export function ResearchEfficiencyPanel({ progress, partial, reason }: {
  progress?: ResearchEfficiency; partial?: boolean; reason?: string;
}) {
  if (!progress && !partial) return null;
  const tasks = Object.values(progress?.tasks ?? {});
  const ended = tasks.filter((task) => task.status === "completed").length;
  const admitted = tasks.filter((task) => ["accepted", "accepted_with_caveats"].includes(task.admission_status ?? "")).length;
  const gaps = Object.values(progress?.requirements ?? {}).filter((item) => item.status !== "supported");
  return <section className="research-efficiency-panel" aria-label="资料检查与研究收敛">
    <strong>{partial ? progress?.admitted_count ? "部分研究已完成" : "资料不足，尚无已准入结论" : "资料检查与研究进展"}</strong>
    {partial && <p role="status">{reasons[reason ?? ""] ?? "当前资料不足以完成全部要求。"}未证实内容不会作为结论。</p>}
    {progress && <>
      <p>已获取 {progress.document_count} 份资料 · 已检查 {progress.processed_chunks} / {progress.total_chunks} 个片段 · {progress.admitted_count} 条已准入证据</p>
      <p>研究员执行结束 {ended} 项 · 交接通过 {admitted} 项</p>
      <p className="efficiency-reuse">文档复用 {progress.counters.document_cache_hits ?? 0} 次 · 提取复用 {progress.counters.extraction_cache_hits ?? 0} 次 · 评估复用 {progress.counters.assessment_cache_hits ?? 0} 次</p>
      {gaps.length > 0 && <details><summary>待解决问题（{gaps.length}）</summary><ul>{gaps.map((item) => <li key={item.text}><strong>{item.text}</strong><span>已补证 {item.supplement_rounds} 轮</span>{item.gaps.map((gap, index) => <p key={index}>{gap.reason}</p>)}</li>)}</ul></details>}
    </>}
  </section>;
}
