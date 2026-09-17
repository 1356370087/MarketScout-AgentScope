"use client";

import { Activity, AlertTriangle, CircleStop, Download, ExternalLink, FileText, Search, Send, ShieldCheck } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { ApprovalCenter } from "@/features/research/approval-center";
import { AppShell } from "@/components/app-shell";
import { ReportPublications } from "@/features/research/report-publications";
import { RunQualityStatus } from "@/components/run-quality-status";
import { ResearchTeamPanel } from "@/features/research/research-team-panel";
import { TaskActivityDrawer } from "@/features/research/task-activity-drawer";
import { TokenUsageDashboard, UsageCompactSummary } from "@/components/token-usage-dashboard";
import { useRunStream } from "@/hooks/use-run-stream";
import { researchApi } from "@/lib/api";
import { isSecurityApprovalResolved, shouldRequestSecurityApprovals } from "@/lib/security-approvals";
import { deriveWaveStatus, STAGES } from "@/lib/run-reducer";
import type { ResearchTask } from "@/lib/types";
import { useResearchRunStore } from "@/stores/research-run-store";

import { MarkdownReport, ReportReviewPanel } from "./report-presentation";
export { ReportReviewPanel } from "./report-presentation";

const stageNames = { preparing: "准备", planning: "规划", researching: "研究", synthesizing: "汇总", writing: "撰写", finalizing: "完成" };

export { HumanActionCard, SecurityApprovalCard } from "@/features/research/approval-center";

function TaskCard({ task, selected, onOpen }: { task: ResearchTask; selected: boolean; onOpen: () => void }) { return <button type="button" data-task-id={task.task_id} className={`task-card task-card-button ${selected ? "selected" : ""}`} aria-haspopup="dialog" aria-expanded={selected} onClick={onOpen}><div className="task-card-status"><span className="status-chip" data-status={task.status}>{task.status ?? "pending"}</span><Activity size={14} aria-hidden="true" /></div><h3>{task.title ?? task.task_id}</h3><p className="task-activity-label">{task.activity_label ?? task.activity_phase ?? task.phase ?? "等待活动"}</p><div className="task-meta"><span>ITER {task.iteration ?? 0}</span><span>SRC {task.source_count ?? 0}</span><span>MODEL {task.model_call_count ?? 0}</span><span>TOOL {task.tool_call_count ?? 0}</span>{(task.retry_count ?? 0) > 0 && <span>RETRY {task.retry_count}</span>}</div><small className="task-open-hint">打开执行详情 →</small></button>; }

function RunInspector() {
  const state = useResearchRunStore(); const sources = Object.values(state.sourcesById); const findings = Object.values(state.findingsByTaskId);
  return <><h2 className="inspector-title">实时证据面板</h2><UsageCompactSummary runId={state.runId} terminal={state.terminal} /><div className="inspector-block"><div className="summary-grid"><div><b>{Object.keys(state.tasksById).length}</b><span>研究任务</span></div><div><b>{sources.length}</b><span>唯一来源</span></div><div><b>{findings.length}</b><span>发现更新</span></div><div><b>{state.lastEventId}</b><span>事件序号</span></div></div></div>{state.warnings.length > 0 && <div className="inspector-block"><p className="eyebrow">警告</p>{state.warnings.map((warning, index) => <div className="finding" key={`${warning.code}-${index}`}><AlertTriangle size={13} /> {warning.message}</div>)}</div>}<div className="inspector-block"><p className="eyebrow">来源 / 去重后</p>{sources.map((source, index) => <a className="source-item" key={source.source_id || source.url} href={source.url} target="_blank" rel="noopener noreferrer"><span className="source-number">{String(index + 1).padStart(2, "0")}</span><span><b>{source.title || source.domain || source.url}</b><small>{source.domain} · {source.task_id ?? "lead"} <ExternalLink size={9} /></small></span></a>)}{!sources.length && <p className="empty-note">Researcher 发现的来源会在这里按规范化 URL 去重。</p>}</div><div className="inspector-block"><p className="eyebrow">Findings / 按任务更新</p>{findings.map((finding) => <div className="finding" key={finding.task_id}>{finding.summary || "已更新结构化发现"}</div>)}{!findings.length && <p className="empty-note">压缩后的发现会在任务完成时出现。</p>}</div></>;
}

export function ResearchWorkspace({ runId }: { runId: string }) {
  useRunStream(runId);
  const [approvalsOpen, setApprovalsOpen] = useState(false);
  const router = useRouter(); const searchParams = useSearchParams(); const selectedTaskId = searchParams.get("task");
  const state = useResearchRunStore(); const [feedback, setFeedback] = useState("");
  const { connectionState, isHydrated, runId: hydratedRunId, terminal, setSecurityApprovals } = state;
  useEffect(() => {
    let active = true;
    if (terminal) {
      setSecurityApprovals([]);
      return () => { active = false; };
    }
    if (!shouldRequestSecurityApprovals(runId, hydratedRunId, isHydrated, terminal)) return () => { active = false; };
    const controllers = new Set<AbortController>();
    const refresh = () => {
      const sequenceBefore = useResearchRunStore.getState().lastEventId;
      const controller = new AbortController();
      controllers.add(controller);
      void researchApi.securityApprovals(runId, controller.signal).then((response) => {
        if (active && sequenceBefore === useResearchRunStore.getState().lastEventId) setSecurityApprovals(response.approvals.filter((item) => !isSecurityApprovalResolved(item.approval_id)));
      }).catch(() => undefined).finally(() => controllers.delete(controller));
    };
    refresh();
    const timer = window.setInterval(refresh, 3_000);
    return () => { active = false; window.clearInterval(timer); controllers.forEach((controller) => controller.abort()); };
  }, [connectionState, hydratedRunId, isHydrated, runId, setSecurityApprovals, terminal]);
  const requestedView = searchParams.get("view"); const view = requestedView === "usage" || requestedView === "report" ? requestedView : "process";
  const tasks = Object.values(state.tasksById); const selectedTask = selectedTaskId ? state.tasksById[selectedTaskId] : undefined; const waveIds = Array.from(new Set(tasks.map((task) => task.wave_id || "wave-0")));
  const showReport = Boolean(state.report) && view === "report"; const showUsage = view === "usage";
  function downloadMarkdown() { const blob = new Blob([state.report], { type: "text/markdown;charset=utf-8" }); const href = URL.createObjectURL(blob); const anchor = document.createElement("a"); anchor.href = href; anchor.download = `${state.title || runId}.md`; anchor.click(); URL.revokeObjectURL(href); }
  async function submitFeedback() { if (!feedback.trim()) return; await researchApi.feedback(runId, { type: "direction", message: feedback }); setFeedback(""); }
  function selectTask(taskId?: string) { if (taskId) setApprovalsOpen(false); const params = new URLSearchParams(searchParams.toString()); if (taskId) params.set("task", taskId); else params.delete("task"); const query = params.toString(); router.push(`/research/${encodeURIComponent(runId)}${query ? `?${query}` : ""}`, { scroll: false }); }
  function setView(next: "process" | "usage" | "report") { const params = new URLSearchParams(searchParams.toString()); params.set("view", next); router.push(`/research/${encodeURIComponent(runId)}?${params}`, { scroll: false }); }
  return <><AppShell inspector={<RunInspector />}><div className="run-topbar"><div className="run-topbar-row"><div><span className="eyebrow mono">RUN / {runId}</span><h1>{state.title || "研究任务"}</h1></div><div className="run-controls"><button data-approval-trigger className="secondary approval-trigger" onClick={() => setApprovalsOpen(true)} aria-haspopup="dialog" aria-expanded={approvalsOpen}><ShieldCheck size={16} /> 审批中心 <span>{state.pendingSecurityApprovals.length + (state.pendingHumanAction ? 1 : 0)}</span></button><span className={`connection ${state.connectionState}`}><i />{state.connectionState}</span><div className="view-tabs"><button data-task-focus-fallback className={view === "process" ? "active" : ""} onClick={() => setView("process")}>过程</button><button className={showUsage ? "active" : ""} onClick={() => setView("usage")}>用量</button><button className={showReport ? "active" : ""} disabled={!state.report} onClick={() => setView("report")}>报告</button></div><button className="danger" disabled={state.terminal} onClick={() => researchApi.cancel(runId)}><CircleStop size={14} /> 取消</button></div></div><div className="stage-track" aria-label="研究阶段">{STAGES.map((stage) => <div key={stage} className={`stage-node ${state.stageProgress[stage] ?? (state.currentStage === stage ? "running" : "")}`}><span>{stageNames[stage]}</span></div>)}</div></div><div className="run-content"><RunQualityStatus state={state} /><ResearchTeamPanel runId={runId} revision={state.lastEventId} terminal={state.terminal} />{(state.pendingHumanAction || state.pendingSecurityApprovals.length > 0) && <button className="approval-pending-notice" onClick={() => setApprovalsOpen(true)}><ShieldCheck size={18} /><span><strong aria-live="polite">{state.pendingSecurityApprovals.length + (state.pendingHumanAction ? 1 : 0)} 项请求等待你的决定</strong><small>相关研究将在你处理后继续</small></span><span>查看请求 →</span></button>}{state.reportReview && <ReportReviewPanel review={state.reportReview} />}{showUsage ? <TokenUsageDashboard runId={runId} visible terminal={state.terminal} /> : showReport ? <><div className="report-actions" style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}><button className="secondary" onClick={downloadMarkdown}><Download size={15} /> Markdown</button><button className="secondary" onClick={() => window.print()}><FileText size={15} /> 打印 / PDF</button></div><ReportPublications runId={runId} initial={state.publications} preferredFormat={state.preferredOutputFormat} /><article className="report-shell"><MarkdownReport value={state.report} /></article>{state.artifacts.length > 0 && <section className="panel"><div className="panel-header"><h2>Artifacts</h2></div><div className="panel-body task-grid">{state.artifacts.map((artifact, index) => <a className="task-card" key={artifact.path || artifact.url || index} href={artifact.url || artifact.path} download><FileText /> <h3>{artifact.name || artifact.type || `Artifact ${index + 1}`}</h3></a>)}</div></section>}</> : <><section className="panel"><div className="panel-header"><h2>研究计划与任务波次</h2><span className="mono">{tasks.length} TASKS</span></div><div className="panel-body" style={{ display: "grid", gap: 24 }}>{waveIds.map((waveId, waveIndex) => { const waveTasks = tasks.filter((task) => (task.wave_id || "wave-0") === waveId); const waveStatus = deriveWaveStatus(waveTasks, state.wavesById[waveId]?.status); return <div className="wave" key={waveId}><div className="wave-title"><span>WAVE {String(waveIndex + 1).padStart(2, "0")} / {waveId}</span><span>{waveStatus.toUpperCase()}</span></div><div className="task-grid">{waveTasks.map((task) => <TaskCard key={task.task_id} task={task} selected={selectedTaskId === task.task_id} onOpen={() => selectTask(task.task_id)} />)}</div></div>; })}{!tasks.length && <p className="empty-note">等待计划事件。连接建立后，真实任务会按执行 wave 出现在轨道中。</p>}</div></section><section className="panel"><div className="panel-header"><h2>全局方向反馈</h2><Search size={15} /></div><div className="panel-body" style={{ display: "flex", gap: 8 }}><input style={{ flex: 1, background: "#091014", border: "1px solid var(--line)", color: "white", padding: 10 }} value={feedback} onChange={(event) => setFeedback(event.target.value)} placeholder="调整研究方向，或提出需要补证的主张……" /><button className="secondary" disabled={!feedback.trim()} onClick={submitFeedback}><Send size={15} /> 发送</button></div></section></>}</div></AppShell><ApprovalCenter key={runId} runId={runId} open={approvalsOpen} onOpenChange={setApprovalsOpen} /><TaskActivityDrawer runId={runId} task={approvalsOpen ? undefined : selectedTask} onClose={() => selectTask()} /></>;
}
