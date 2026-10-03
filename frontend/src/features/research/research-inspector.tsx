"use client";

import { AlertTriangle, ArrowUpRight } from "lucide-react";
import { useState } from "react";
import { EmptyState, Tabs } from "@/components/ui/workspace";
import { UsageCompactSummary } from "@/components/token-usage-dashboard";
import { useResearchRunStore } from "@/stores/research-run-store";
import type { ResearchSource } from "@/lib/types";
import { ReportContents, ReportReviewPanel } from "./report-presentation";
import { EvidenceExplorer, SourceDetail } from "./evidence/source-card";

export function RunInspector({ report, source, taskId, onSource, onTask, onFindCitation, onClearSource }: {
  report: boolean; source?: ResearchSource; taskId?: string | null; onSource: (source: ResearchSource) => void;
  onTask: (taskId: string) => void; onFindCitation: () => void; onClearSource: () => void;
}) {
  const state = useResearchRunStore();
  const [tab, setTab] = useState("sources");
  if (source) return <SourceDetail source={source} task={source.task_id ? state.tasksById[source.task_id] : undefined} onTask={onTask} onFindCitation={state.report ? onFindCitation : undefined} onClose={onClearSource} />;
  const sources = Object.values(state.sourcesById), tasks = Object.values(state.tasksById);
  return <><h2 className="inspector-title">研究信息</h2>{report && <ReportContents value={state.report} />}
    <Tabs label="研究信息分类" value={tab} onChange={setTab} items={[{ value: "sources", label: `来源 ${sources.length}` }, { value: "findings", label: "发现" }, { value: "quality", label: "质量" }]}>
      {tab === "sources" && <EvidenceExplorer sources={sources} tasks={tasks} onSelect={onSource} compact taskId={taskId} />}
      {tab === "findings" && <div className="inspector-block">{Object.values(state.findingsByTaskId).map((finding) => <article className="finding" key={finding.task_id}><h3>{state.tasksById[finding.task_id]?.title ?? "研究发现"}</h3><p>{finding.summary || "已更新结构化发现"}</p><button className="ui-text-button" onClick={() => onTask(finding.task_id)}>查看研究过程<ArrowUpRight size={13} /></button></article>)}{!Object.keys(state.findingsByTaskId).length && <EmptyState title="尚无阶段发现" description="任务交接后，已记录的发现会显示在这里。" />}</div>}
      {tab === "quality" && <div className="inspector-block">{state.reportReview ? <ReportReviewPanel review={state.reportReview} /> : <EmptyState title="尚无报告复核结果" description="任务执行与质量接纳分别记录。" />}{state.warnings.map((warning, index) => <div className="finding" key={`${warning.code}-${index}`}><AlertTriangle size={15} /> {warning.message}</div>)}</div>}
    </Tabs><UsageCompactSummary runId={state.runId} terminal={state.terminal} />
  </>;
}
