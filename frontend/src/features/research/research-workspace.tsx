"use client";

import { ResearchEfficiencyPanel } from "./research-efficiency-panel";

import { ArrowUp, Check, ChevronRight, CircleStop, Download, FileText, ShieldCheck, Sparkles, Layers, Bot, CheckCircle2 } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { AppShell } from "@/components/app-shell";
import { EmptyState, StatusBadge, SurfaceDialog, Tabs } from "@/components/ui/workspace";
import { ApprovalCenter } from "@/features/research/approval-center";
import { ReportPublications } from "@/features/research/report-publications";
import { RunQualityStatus } from "@/components/run-quality-status";
import { ResearchTeamPanel } from "@/features/research/research-team-panel";
import { TaskActivityDrawer } from "@/features/research/task-activity-drawer";
import { TokenUsageDashboard } from "@/components/token-usage-dashboard";
import { useRunStream } from "@/hooks/use-run-stream";
import { useRunUsage } from "@/hooks/use-run-usage";
import { researchApi } from "@/lib/api";
import { isSecurityApprovalResolved, shouldRequestSecurityApprovals } from "@/lib/security-approvals";
import { deriveWaveStatus, STAGES } from "@/lib/run-reducer";
import type { ResearchSource } from "@/lib/types";
import { useResearchRunStore } from "@/stores/research-run-store";
import { MarkdownReport, ReportReviewPanel } from "./report-presentation";
import { MetricStrip, LoadingSkeleton, Disclosure, StructuredContent } from "@/components/ui/insight";
import { AgentTaskCard } from "./agents/agent-task-card";
import { EvidenceExplorer } from "./evidence/source-card";
import { RunInspector } from "./research-inspector";
import { matchSource, sourceKey } from "./activity/presentation";
import "./research-ui.css";
export { ReportReviewPanel } from "./report-presentation";
export { HumanActionCard, SecurityApprovalCard } from "@/features/research/approval-center";

const stageNames = { preparing: "准备", planning: "规划", researching: "研究", synthesizing: "汇总", writing: "撰写", finalizing: "完成" };
const connectionNames = { idle: "等待连接", connecting: "连接中", connected: "实时连接", reconnecting: "正在重连", closed: "连接已关闭", error: "连接异常" };

export function ResearchWorkspace({ runId }: { runId: string }) {
  useRunStream(runId);
  const inspectorDestination = useRef<{ type: "citation"; key: string } | { type: "task" } | null>(null);
  const [approvalsOpen, setApprovalsOpen] = useState(false);
  const searchParams = useSearchParams();
  const [inspectorOpen, setInspectorOpen] = useState(Boolean(searchParams.get("source")));
  const [activitySource, setActivitySource] = useState<{ runId: string; source: ResearchSource }>();
  const [citationNotice, setCitationNotice] = useState("");
  const router = useRouter(), selectedTaskId = searchParams.get("task");
  const state = useResearchRunStore();
  const [feedback, setFeedback] = useState("");
  const [sending, setSending] = useState(false);
  const [feedbackError, setFeedbackError] = useState("");
  const [feedbackNotice, setFeedbackNotice] = useState("");
  const [cancelOpen, setCancelOpen] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState("");
  const usage = useRunUsage(runId, true, state.terminal);
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
  const requestedView = searchParams.get("view");
  const view = ["usage", "report", "team", "evidence"].includes(requestedView ?? "") ? requestedView! : "process";
  const tasks = useMemo(() => Object.values(state.tasksById).map((task) => ({ ...task, ...usage.data?.task_operations?.[task.task_id] })), [state.tasksById, usage.data?.task_operations]);
  const sources = useMemo(() => Object.values(state.sourcesById), [state.sourcesById]);
  const selectedSourceId = searchParams.get("source");
  const selectedSource = sources.find((source) => sourceKey(source) === selectedSourceId)
    ?? (activitySource?.runId === runId && sourceKey(activitySource.source) === selectedSourceId ? activitySource.source : undefined);
  const citation = searchParams.get("citation");
  useEffect(() => {
    if (view !== "report" || !citation) return;
    const frame = requestAnimationFrame(() => locateCitation(citation));
    return () => cancelAnimationFrame(frame);
  }, [view, citation, state.report]);
  function locateCitation(key: string) {
    const target = document.querySelector<HTMLAnchorElement>(`.report-shell [data-report-source="${CSS.escape(key)}"]`);
    setCitationNotice(target ? "已定位报告中的引用。" : "报告中尚无可识别的关联引用，可直接阅读来源原文。");
    if (target) { target.focus({ preventScroll: true }); target.scrollIntoView({ block: "center", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth" }); }
  }
  function selectSource(picked: ResearchSource) {
    const source = sources.find((item) => sourceKey(item) === sourceKey(picked)) ?? matchSource(picked.url, sources) ?? picked;
    inspectorDestination.current = null;
    setActivitySource({ runId, source }); setApprovalsOpen(false); setCitationNotice("");
    const params = new URLSearchParams(searchParams.toString());
    params.set("source", sourceKey(source)); params.delete("task"); params.delete("citation");
    router.push(`/research/${encodeURIComponent(runId)}?${params}`, { scroll: false });
    setInspectorOpen(true);
  }
  function clearSource() {
    const params = new URLSearchParams(searchParams.toString()); params.delete("source"); params.delete("citation");
    router.push(`/research/${encodeURIComponent(runId)}?${params}`, { scroll: false });
  }
  function findCitation() {
    if (!selectedSource) return;
    inspectorDestination.current = { type: "citation", key: sourceKey(selectedSource) };
    setInspectorOpen(false);
    if (view === "report") { locateCitation(sourceKey(selectedSource)); return; }
    const params = new URLSearchParams(searchParams.toString()); params.set("view", "report"); params.set("citation", sourceKey(selectedSource)); params.delete("task");
    router.push(`/research/${encodeURIComponent(runId)}?${params}`, { scroll: false });
  }
  const selectedTask = tasks.find((task) => task.task_id === selectedTaskId);
  const waveIds = Array.from(new Set(tasks.map((task) => task.wave_id || "wave-0")));
  const pendingCount = state.pendingSecurityApprovals.length + (state.pendingHumanAction ? 1 : 0);
  function downloadMarkdown() { const blob = new Blob([state.report], { type: "text/markdown;charset=utf-8" }); const href = URL.createObjectURL(blob); const anchor = document.createElement("a"); anchor.href = href; anchor.download = `${state.title || runId}.md`; anchor.click(); URL.revokeObjectURL(href); }
  async function submitFeedback() {
    if (!feedback.trim() || sending || state.terminal) return;
    setSending(true); setFeedbackError(""); setFeedbackNotice("");
    try { await researchApi.feedback(runId, { type: "direction", message: feedback }); setFeedback(""); setFeedbackNotice("补充已受理，实际应用情况请查看研究进展。"); }
    catch (error) { setFeedbackError(error instanceof Error ? error.message : "发送失败，请重试。"); }
    finally { setSending(false); }
  }
  async function cancel() { setCancelling(true); setCancelError(""); try { await researchApi.cancel(runId); setCancelOpen(false); } catch (error) { setCancelError(error instanceof Error ? error.message : "停止失败，请重试。"); } finally { setCancelling(false); } }
  function selectTask(taskId?: string) { inspectorDestination.current = taskId ? { type: "task" } : null; setInspectorOpen(false); if (taskId) setApprovalsOpen(false); const params = new URLSearchParams(searchParams.toString()); if (taskId) { params.set("task", taskId); params.delete("source"); params.delete("citation"); } else params.delete("task"); const query = params.toString(); router.push(`/research/${encodeURIComponent(runId)}${query ? `?${query}` : ""}`, { scroll: false }); }
  function setView(next: string) { const params = new URLSearchParams(searchParams.toString()); params.set("view", next); router.push(`/research/${encodeURIComponent(runId)}?${params}`, { scroll: false }); }
  const taskWaves = waveIds.map((waveId, index) => { const waveTasks = tasks.filter((task) => (task.wave_id || "wave-0") === waveId); return <section className="research-wave" key={waveId}><div className="research-wave-heading"><h3>第 {index + 1} 组任务</h3><StatusBadge status={deriveWaveStatus(waveTasks, state.wavesById[waveId]?.status)} /></div><div className="agent-task-grid">{waveTasks.map((task) => <AgentTaskCard key={task.task_id} task={task} selected={selectedTaskId === task.task_id} onOpen={() => selectTask(task.task_id)} />)}</div></section>; });
  const approvalNotice = pendingCount > 0 && <button className="approval-pending-notice" onClick={() => setApprovalsOpen(true)}><ShieldCheck size={20} /><span><strong aria-live="polite">{pendingCount} 项请求等待你的决定</strong><small>处理后，相关研究才能继续</small></span><ChevronRight size={17} /></button>;
  return <><AppShell onInspectorCloseAutoFocus={(event) => {
    const destination = inspectorDestination.current;
    if (!destination) return;
    event.preventDefault(); inspectorDestination.current = null;
    if (destination.type === "citation") requestAnimationFrame(() => locateCitation(destination.key));
  }} inspectorOpen={inspectorOpen} onInspectorOpenChange={setInspectorOpen} inspector={<RunInspector report={view === "report"} source={selectedSource} taskId={selectedTaskId} onSource={selectSource} onTask={selectTask} onFindCitation={findCitation} onClearSource={clearSource} />}><div className="research-heading"><div><span className="eyebrow">深度研究</span><h1>{state.title || "研究任务"}</h1><div className="research-meta"><StatusBadge status={state.status} /><span>{tasks.length} 个任务 · {Object.keys(state.sourcesById).length} 个来源</span><span className={`connection ${state.connectionState}`}><i />{connectionNames[state.connectionState]}</span></div></div><button data-approval-trigger data-task-focus-fallback className="secondary approval-trigger" onClick={() => setApprovalsOpen(true)} aria-haspopup="dialog" aria-expanded={approvalsOpen}><ShieldCheck size={16} /> 审批中心 <span>{pendingCount}</span></button></div>
    {(state.connectionState === "reconnecting" || state.connectionState === "error") && <p className="research-connection-notice" role="status">连接中断，保留最后已知状态，正在尝试恢复实时更新。</p>}
    <Tabs label="研究视图" value={view} onChange={setView} items={[{ value: "process", label: "研究进展" }, { value: "team", label: "研究团队" }, { value: "evidence", label: "来源证据" }, { value: "report", label: "报告", disabled: !state.report }, { value: "usage", label: "用量" }]}><div className="research-content">
      <RunQualityStatus state={state} />
      {(view === "process" || state.completionStatus === "partial") && <ResearchEfficiencyPanel
        progress={usage.data?.research_progress ?? state.efficiency}
        partial={state.completionStatus === "partial"} reason={state.stopReason} />}
      {citationNotice && <p role="status" className="citation-notice">{citationNotice}</p>}
      {view !== "process" && approvalNotice}
      {view === "team" ? <ResearchTeamPanel runId={runId} revision={state.lastEventId} terminal={state.terminal} onTask={selectTask} activityTaskIds={tasks.map((task) => task.task_id)} /> : view === "evidence" ? <EvidenceExplorer sources={sources} tasks={tasks} selectedSource={selectedSourceId} onSelect={selectSource} /> : view === "usage" ? <TokenUsageDashboard runId={runId} visible terminal={state.terminal} onTask={selectTask} taskIds={tasks.map((task) => task.task_id)} /> : view === "report" ? state.report ? <>
        <div className="report-actions"><SurfaceDialog title="导出研究报告" description="选择交付格式，查看生成进度与发布结果。" trigger={<button className="secondary"><Download size={16} /> 导出报告</button>}><div className="ui-actions"><button className="secondary" onClick={downloadMarkdown}><Download size={15} /> Markdown</button><button className="secondary" onClick={() => window.print()}><FileText size={15} /> 打印 / PDF</button></div><ReportPublications runId={runId} initial={state.publications} preferredFormat={state.preferredOutputFormat} /></SurfaceDialog></div>
        {state.reportReview && <ReportReviewPanel review={state.reportReview} />}<article className="report-shell report-reader"><MarkdownReport value={state.report} sources={sources} selectedSource={selectedSourceId} onSource={selectSource} /></article>
        {state.artifacts.length > 0 && <section className="panel"><div className="panel-header"><h2>研究附件</h2></div><div className="panel-body task-grid">{state.artifacts.map((artifact, index) => <a className="task-card" key={artifact.path || artifact.url || index} href={artifact.url || artifact.path} download><FileText /><h3>{artifact.name || artifact.type || `附件 ${index + 1}`}</h3></a>)}</div></section>}
      </> : <EmptyState title="报告尚未生成" description="研究完成撰写后，报告会显示在这里。" /> : <>
        <ol className="research-stage-track" aria-label="研究阶段">{STAGES.map((stage) => <li key={stage} data-status={state.stageProgress[stage] ?? (state.currentStage === stage ? "running" : "pending")} aria-current={state.currentStage === stage ? "step" : undefined}>{stageNames[stage]}</li>)}</ol>
        {approvalNotice}
        {Object.keys(state.plan).length > 0 && <Disclosure title="研究计划"><StructuredContent value={state.plan} /></Disclosure>}
        {!state.isHydrated ? <LoadingSkeleton label="正在读取研究状态…" /> : tasks.length > 0 && <MetricStrip label="研究概况" items={[
          { label: "研究任务", value: tasks.length, icon: <Bot size={14} /> },
          { label: "已完成任务", value: `${tasks.filter((task) => task.status === "completed").length} / ${tasks.length}`, icon: <CheckCircle2 size={14} /> },
          { label: "已记录来源", value: sources.length, icon: <Layers size={14} /> },
        ]} />}
        <ol className="research-timeline">{STAGES.filter((stage) => state.stageProgress[stage] === "completed" || state.stageProgress[stage] === "failed" || state.currentStage === stage || (stage === "researching" && tasks.length > 0)).map((stage) => <li className="research-step" key={stage}><span className="research-step-icon">{state.stageProgress[stage] === "completed" ? <Check size={17} /> : <Sparkles size={17} />}</span><div><h2>{stageNames[stage]}<StatusBadge status={state.stageProgress[stage] ?? (state.currentStage === stage ? "running" : "pending")} label={!state.stageProgress[stage] && state.currentStage !== stage ? "已记录任务" : undefined} /></h2>{stage === "researching" ? taskWaves.length ? taskWaves : <p className="empty-note">等待研究任务分配。</p> : <p className="research-step-copy">{state.stageProgress[stage] === "completed" ? "这一阶段已完成。" : state.stageProgress[stage] === "failed" ? "这一阶段未完成，请查看运行提示。" : "正在处理这一阶段，进展将持续更新。"}</p>}</div></li>)}</ol>

        {!state.currentStage && !tasks.length && state.isHydrated && <EmptyState title={state.terminal ? "没有已记录的研究任务" : "研究准备中"} description="计划与任务由实际运行记录生成。" />}
        {!state.terminal && <form className="direction-composer" onSubmit={(event) => { event.preventDefault(); void submitFeedback(); }}><label htmlFor="direction-feedback">补充研究方向</label><textarea id="direction-feedback" value={feedback} disabled={sending} onChange={(event) => setFeedback(event.target.value)} placeholder="调整研究方向，或提出需要补证的主张……" rows={2} /><div className="direction-footer"><span>发送给本次研究</span><div className="ui-actions"><button type="button" className="ui-text-button" disabled={state.status === "cancelling"} onClick={() => setCancelOpen(true)}><CircleStop size={15} /> 停止研究</button><button type="submit" className="primary" aria-label="发送补充" disabled={sending || !feedback.trim()}><ArrowUp size={16} />{sending ? "发送中" : "发送"}</button></div></div>{feedbackError && <p role="alert" className="form-alert error">{feedbackError}</p>}{feedbackNotice && <p role="status" className="empty-note">{feedbackNotice}</p>}</form>}
      </>}
    </div></Tabs>
  </AppShell><ApprovalCenter key={runId} runId={runId} open={approvalsOpen} onOpenChange={setApprovalsOpen} onTask={selectTask} /><TaskActivityDrawer runId={runId} onSource={selectSource} task={approvalsOpen ? undefined : selectedTask} onClose={() => selectTask()} />
  <SurfaceDialog title="停止这次研究？" description="已产生的研究记录会保留。停止后不能继续本次执行。" open={cancelOpen} onOpenChange={setCancelOpen}>{cancelError && <p role="alert" className="form-alert error">{cancelError}</p>}<div className="ui-actions"><button className="secondary" onClick={() => setCancelOpen(false)}>继续研究</button><button className="danger" disabled={cancelling || state.terminal} onClick={() => void cancel()}>{cancelling ? "正在停止…" : "确认停止"}</button></div></SurfaceDialog></>;
}
