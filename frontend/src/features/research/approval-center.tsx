"use client";

import "./approval-center.css";

import * as Dialog from "@radix-ui/react-dialog";
import { Check, CheckCheck, ChevronRight, ExternalLink, Globe2, LoaderCircle, MessageSquareText, ShieldCheck, ShieldQuestion, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import { StatusBadge } from "@/components/ui/workspace";
import { researchApi } from "@/lib/api";
import { isSecurityApprovalResolved, resolvedSecurityApprovalIds, resolvedHumanActionIds } from "@/lib/security-approvals";
import type { EgressState, EgressTarget, PendingHumanAction, SecurityApproval } from "@/lib/types";
import { useResearchRunStore } from "@/stores/research-run-store";

const names: Record<string, string> = {
  network: "网络访问", tool_effect: "工具操作", command: "命令执行", filesystem: "文件访问", mcp_oauth: "服务授权",
  clarification: "补充信息", plan_approval: "研究计划", outline_approval: "报告大纲", fetch_budget_approval: "抓取预算",
};
const modes = { manual: "逐项人工", auto: "自动分类", open: "全部放行" };
const capabilities: Record<string, string> = { "tool.egress": "只读网页访问", "tool.network": "工具网络访问", "proxy.connect": "代理连接", "external.extract": "外部提取服务", "browser.navigate": "浏览器访问" };
const reasons: Record<string, string> = {
  mode: "当前模式要求逐项人工确认。", mode_changed: "访问策略已收窄，需要重新确认。",
  human_revoked: "这个目标的许可已撤销，需要重新确认。",
  capability_requires_human: "这类访问不支持自动分类，需要单独授权。",
  classifier_unavailable: "分类服务暂不可用，需要人工确认。",
  budget_exhausted: "本次研究的分类预算已耗尽，需要人工确认。",
  degraded: "分类服务已降级，需要人工确认。",
  uncertain: "目标风险尚不确定，需要人工确认。",
  classifier: "自动分类尚不能确认这个目标的访问风险，需要你决定。",
  ledger: "这个目标已有待确认的分类结果，需要你决定。",
  classifier_budget_exhausted: "本次研究的分类预算已耗尽，需要人工确认。",
  classifier_degraded: "分类服务已降级，需要人工确认。",
  timeout: "分类请求超时，需要人工确认。",
  error: "分类服务调用失败，需要人工确认。",
  stage1_unparseable: "分类结果无法解析，需要人工确认。",
};
const targetDecisions = { allow_run: "本次研究允许", block_run: "本次研究阻止", revoke: "撤销并转人工" };
const inFlight = new Set<string>();

export function ApprovalTrigger({ open, onClick }: { open: boolean; onClick: () => void }) {
  const human = useResearchRunStore(s => s.pendingHumanAction);
  const approvals = useResearchRunStore(s => s.pendingSecurityApprovals);
  const tasks = new Set(approvals.map(a => a.task_id));
  const total = approvals.length + (human ? 1 : 0);
  const [now, setNow] = useState<number>();
  useEffect(() => { setNow(Date.now()); if (!total) return; const timer = setInterval(() => setNow(Date.now()), 30_000); return () => clearInterval(timer); }, [total]);
  const oldest = Math.min(...approvals.map(a => a.requested_at));
  const wait = now && Number.isFinite(oldest) ? Math.max(0, Math.floor((now / 1000 - oldest) / 60)) : undefined;
  return <button data-approval-trigger data-task-focus-fallback className={`approval-trigger approval-summary ${total ? "has-pending" : ""}`} onClick={onClick} aria-label={`审批中心 ${total}`} aria-haspopup="dialog" aria-expanded={open}>
    <span className="approval-summary-icon"><ShieldCheck size={20} /></span><span className="approval-summary-copy"><strong>审批中心 <em>{total}</em></strong><small>{total ? `${human ? names[human.type] + "待确认 · " : ""}${approvals.length ? approvals.length + " 项访问请求" : "研究等待你的决定"}` : "暂无待处理事项"}</small>{total > 0 && <small>{tasks.size > 0 ? `关联 ${tasks.size} 个任务` : "确认后继续研究"}{wait !== undefined && ` · 最长等待 ${wait} 分钟`}</small>}</span><ChevronRight size={16} />
  </button>;
}

function SourcePlanDetails({ action, edit, onEdit, readOnly = false }: { action: PendingHumanAction; edit: string | null; onEdit: (value: string | null) => void; readOnly?: boolean }) {
  const plan = action.payload.source_plan;
  const requirements = action.payload.requirements ?? [];
  return <div className="approval-plan-summary">{requirements.length > 0 && <section><h4>研究范围与交付要求</h4>{["factual", "deliverable", "process"].map(kind => <div key={kind}><span className="approval-badge">{kind === "factual" ? "研究问题" : kind === "deliverable" ? "交付要求" : "来源与执行规则"}</span><ul>{requirements.filter(r => r.kind === kind).map(r => <li key={r.requirement_id}>{r.text}</li>)}</ul></div>)}</section>}
    {plan && <section><h4>{plan.intent === "explicit" ? "指定来源计划" : "官网来源计划"} <small>版本 {plan.version}</small></h4><p className="approval-muted">{plan.explicit ? "沿用你明确指定的来源。" : plan.status === "verified" || plan.status === "automatic" ? "已核验官网归属与文档链接，确认后用于正式取证。" : "以下候选仍需你确认或纠正。"} 来源确认后仍按访问策略授权。</p>{plan.entries.map((entry, index) => <article className="approval-source-entry" key={`${entry.entity}:${index}`}><div><strong>{entry.entity}</strong><span className="approval-badge">{entry.status === "verified" ? "归属已核验" : entry.status === "confirmed" ? "人工已确认" : "待确认"}</span></div>{entry.website ? <a href={entry.website} target="_blank" rel="noopener noreferrer">{entry.website}<ExternalLink size={13} /></a> : <p className="approval-error">未发现可核验官网，请补充域名。</p>}{entry.documentation_urls.length > 0 && <ul>{entry.documentation_urls.map(url => <li key={url}><a href={url} target="_blank" rel="noopener noreferrer">{url}</a></li>)}</ul>}<details><summary>查看核验依据</summary><p>{entry.verification_basis?.entity_quote}</p><p>{entry.verification_basis?.ownership_quote}</p><p className="approval-muted">{entry.verification_basis?.reason ?? entry.reason ?? entry.discovery_basis}</p></details></article>)}{plan.explicit && <pre className="approval-selected-sources">{plan.selection?.sources.map(s => s.type === "domain" ? s.domain : s.type === "url" ? s.url : "已选企业资料").join("\n")}</pre>}{!readOnly && <details><summary>调整来源域名</summary><label className="approval-message">本次研究允许的域名<textarea aria-label="调整来源域名" value={edit ?? plan.entries.map(e => e.domain ?? "").filter(Boolean).join("\n")} onChange={e => onEdit(e.target.value)} placeholder="每行一个域名，例如 example.com" /></label><p className="approval-muted">填写后会以指定域名范围替换本次来源选择；允许列表不要求每个域名都被引用。</p>{edit !== null && <button className="ui-text-button" onClick={() => onEdit(null)}>恢复候选来源</button>}</details>}</section>}
  </div>;
}

function errorMessage(cause: unknown): string {
  const message = cause instanceof Error ? cause.message : "";
  if (message.startsWith("403:")) return "你没有处理这项请求的权限，或管理员策略不允许此操作。";
  if (message.startsWith("409:")) return "状态已发生变化，请查看最新状态后重试。";
  return "提交未完成，请重试。当前请求仍由服务端状态决定。";
}

function ApprovalMarkdown({ children }: { children: string }) {
  return <div className="approval-markdown"><ReactMarkdown skipHtml remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSanitize]}>{children}</ReactMarkdown></div>;
}

export function SecurityApprovalCard({ runId, approval, disabled = false, onResolved, onTask, related = [] }: {
  runId: string; approval: SecurityApproval; disabled?: boolean; onResolved?: () => void; onTask?: (id: string) => void;
  related?: SecurityApproval[];
}) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const task = useResearchRunStore((state) => state.tasksById[approval.task_id]);
  const key = `${runId}:${approval.approval_id}`;
  async function decide(decision: "allow_once" | "allow_run" | "deny") {
    const store = useResearchRunStore.getState();
    if (disabled || store.runId !== runId || inFlight.has(key) || !store.pendingSecurityApprovals.some((a) => a.approval_id === approval.approval_id)) return;
    inFlight.add(key); setBusy(true); setError("");
    let succeeded = false;
    try {
      await researchApi.resolveSecurityApproval(runId, approval.approval_id, decision, reason);
      succeeded = true;
      resolvedSecurityApprovalIds.add(approval.approval_id);
      if (useResearchRunStore.getState().runId !== runId) return;
      useResearchRunStore.getState().setSecurityApprovals(useResearchRunStore.getState().pendingSecurityApprovals.filter((a) => a.approval_id !== approval.approval_id));
      onResolved?.();
    } catch (cause) {
      if (useResearchRunStore.getState().runId !== runId) return;
      setError(errorMessage(cause));
    } finally {
      // Reconcile errors by querying authority, never by interpreting every 400 as success.
      try {
        const sequenceBefore = useResearchRunStore.getState().lastEventId;
        const current = await researchApi.securityApprovals(runId);
        if (useResearchRunStore.getState().runId === runId && useResearchRunStore.getState().lastEventId === sequenceBefore) {
          useResearchRunStore.getState().setSecurityApprovals(current.approvals.filter((a) => !isSecurityApprovalResolved(a.approval_id)));
          if (!succeeded && !current.approvals.some((a) => a.approval_id === approval.approval_id)) onResolved?.();
        }
      } catch { /* The successful mutation remains authoritative; failed requests remain visible. */ }
      inFlight.delete(key); setBusy(false);
    }
  }
  const network = approval.kind === "network";
  const target = network ? String(approval.target.domain ?? "未知目标") : String(approval.target.command ?? approval.target.path ?? approval.target.tool ?? names[approval.kind]);
  const interactionUrl = approval.kind === "mcp_oauth" && typeof approval.target.url === "string" && /^https?:\/\//.test(approval.target.url) ? approval.target.url : undefined;
  return <article className="approval-detail" aria-busy={busy}>
    <div className="approval-detail-body">
      <span className="approval-kicker">{names[approval.kind]} · 等待你的决定</span>
      <h3>{network ? "允许访问这个网站吗？" : `确认${names[approval.kind]}请求`}</h3>
      <div className="approval-target"><span className="approval-target-icon"><Globe2 size={20} /></span><div><strong>{target}</strong><small>{capabilities[approval.capability] ?? names[approval.kind]}{network && ` · 端口 ${approval.target.port}`}</small></div></div>
      <div className="approval-explanation"><ShieldQuestion size={17} /><p>{(reasons[approval.reason ?? ""] ?? approval.reason) || (network ? "此目标需要人工确认。系统会在你决定后继续相关研究。" : "这项操作需要你的授权才能继续。")}</p></div>
      <div className="approval-scope"><h4>这次批准会影响什么</h4><p>{network ? "允许此次访问仅用于当前操作；本次研究允许仅适用于这个完整域名、端口和访问能力，不包含其他子域名。" : "允许一次仅用于当前操作；本次研究允许会复用于同范围操作。"} 不会修改管理员的永久策略。</p></div>
      {related.length > 1 && <div className="approval-group-context"><strong>同一目标有 {related.length} 项请求</strong><p>来自 {new Set(related.map(a => a.task_id)).size} 个研究任务；当前操作只处理选中的请求，本次研究许可会供相同目标后续使用。</p><ul>{related.map(a => <li key={a.approval_id}>{a.capability} · {a.task_id === approval.task_id ? "当前任务" : "关联任务"}</li>)}</ul></div>}
      {interactionUrl && <a className="secondary approval-oauth" href={interactionUrl} target="_blank" rel="noopener noreferrer">打开服务授权页面 <ExternalLink size={14} /></a>}
      {task && <div className="approval-task-context"><span>关联研究任务</span><strong>{task.title ?? task.task_id}</strong><StatusBadge status={task.status ?? "pending"} />{task.activity_label && <p>{task.activity_label}</p>}{onTask && <button type="button" className="ui-text-button" onClick={() => onTask(task.task_id)}>查看任务过程 <ChevronRight size={14} /></button>}</div>}
      <details className="approval-notes"><summary>添加备注（可选）</summary><textarea aria-label="审批备注" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="记录判断依据，方便之后查看" /></details>
      <details className="approval-technical"><summary>技术信息</summary><dl><dt>任务</dt><dd>{approval.task_id}</dd><dt>请求</dt><dd>{approval.approval_id}</dd><dt>能力</dt><dd>{approval.capability}</dd></dl><pre>{JSON.stringify(approval.target, null, 2)}</pre></details>
      {disabled && <p className="approval-muted">当前仅可查看，无法处理这项请求。</p>}
      {error && <p className="approval-error" role="alert">{error}</p>}
    </div>
    <footer className="approval-actions"><button className="approval-reject" disabled={disabled || busy} onClick={() => decide("deny")}>{network ? "拒绝此次访问" : "拒绝此次操作"}</button><div><button className="secondary" disabled={disabled || busy} onClick={() => decide("allow_run")}><CheckCheck size={15} /> 本次研究允许</button><button className="primary" disabled={disabled || busy} onClick={() => decide("allow_once")}>{busy ? <LoaderCircle className="approval-spin" size={15} /> : <Check size={15} />}{network ? "允许此次访问" : "允许一次"}</button></div></footer>
  </article>;
}

export function HumanActionCard({ runId, action, disabled = false, onResolved }: { runId: string; action: PendingHumanAction; disabled?: boolean; onResolved?: () => void }) {
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [sourceEdit, setSourceEdit] = useState<string | null>(null);
  const clarification = action.type === "clarification";
  const budget = action.type === "fetch_budget_approval";
  const allowed = action.allowed_actions ?? (clarification ? ["answer", "cancel"] : budget ? ["approve", "deny", "cancel"] : ["approve", "revise", "cancel"]);
  const content = action.payload.question ?? action.payload.content_markdown ?? action.payload.research_plan ?? action.payload.report_outline ?? "";
  async function submit(kind: string) {
    if (disabled || busy || inFlight.has(`${runId}:${action.action_id}`) || useResearchRunStore.getState().runId !== runId) return;
    inFlight.add(`${runId}:${action.action_id}`);
    setBusy(true); setError("");
    try {
      const source_selection = sourceEdit === null ? undefined : { mode: "specific" as const, sources: sourceEdit.split(/[\s,，]+/).filter(Boolean).map(domain => ({ type: "domain" as const, domain })) };
      await researchApi.humanAction(runId, action.action_id, kind, message, { source_selection, expected_version: action.payload.version });
      resolvedHumanActionIds.add(`${runId}:${action.action_id}`);
      if (useResearchRunStore.getState().runId !== runId) return;
      if (useResearchRunStore.getState().pendingHumanAction?.action_id === action.action_id) useResearchRunStore.setState({ pendingHumanAction: undefined });
      onResolved?.();
    } catch (cause) { if (useResearchRunStore.getState().runId === runId) setError(errorMessage(cause)); } finally { inFlight.delete(`${runId}:${action.action_id}`); setBusy(false); }
  }
  return <article className="approval-detail" aria-busy={busy}><div className="approval-detail-body"><span className="approval-kicker">{names[action.type]} · 等待你的决定</span><h3>{clarification ? "帮助我们明确研究方向" : budget ? "是否继续收集更多资料？" : `请审阅${names[action.type]}`}</h3><ApprovalMarkdown>{content}</ApprovalMarkdown>{action.type === "plan_approval" && <SourcePlanDetails action={action} edit={sourceEdit} onEdit={setSourceEdit} />}{(allowed.includes("answer") || allowed.includes("revise")) && <label className="approval-message">{clarification ? "补充信息" : "修改意见"}<textarea value={message} onChange={(e) => setMessage(e.target.value)} placeholder={clarification ? "补充你希望研究的范围和重点" : "说明需要调整的内容"} /></label>}{budget && <p className="approval-muted">不增加预算时，会使用现有资料生成部分报告。</p>}{error && <p className="approval-error" role="alert">{error}</p>}</div><footer className="approval-actions">{allowed.includes("cancel") && <button className="approval-reject" disabled={disabled || busy} onClick={() => submit("cancel")}>取消整项研究</button>}<div>{allowed.includes("deny") && <button className="secondary" disabled={disabled || busy} onClick={() => submit("deny")}>不增加，生成部分报告</button>}{allowed.includes("revise") && <button className="secondary" disabled={disabled || busy || !message.trim()} onClick={() => submit("revise")}>提交修改</button>}{allowed.includes("approve") && <button className="primary" disabled={disabled || busy} onClick={() => submit("approve")}>{busy && <LoaderCircle size={15} className="approval-spin" />}{budget ? "增加预算并继续" : "批准并继续"}</button>}{allowed.includes("answer") && <button className="primary" disabled={disabled || busy || !message.trim()} onClick={() => submit("answer")}>回答并继续</button>}</div></footer></article>;
}

function TargetPermission({ runId, target, disabled, refresh }: { runId: string; target: EgressTarget; disabled: boolean; refresh: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [reason, setReason] = useState("");
  const verdict = target.policy_denied ? "管理员策略阻止" : target.decision === "allow_run" ? "人工允许" : target.decision === "block_run" ? "人工阻止" : target.decision === "revoke" ? "已撤销，转人工" : target.classification?.verdict === "allow" ? "模型允许" : target.classification?.verdict === "deny" ? "模型拒绝" : "待人工确认";
  async function decide(decision: "allow_run" | "block_run" | "revoke") {
    setBusy(true); setError("");
    try { await researchApi.decideEgressTarget(runId, target, decision, reason); refresh(); }
    catch (cause) { setError(errorMessage(cause)); refresh(); } finally { setBusy(false); }
  }
  return <article className="approval-permission"><div><strong>{target.target.domain}</strong><span className="approval-badge">{verdict}</span></div><small>{capabilities[target.capability] ?? target.capability} · {target.target.port}</small>{(target.reason || target.classification?.reason) && <p>{target.reason || target.classification?.reason}</p>}<p className="approval-muted">更新于 {new Date(target.updated_at * 1000).toLocaleString("zh-CN")} · 版本 {target.version}</p><details><summary>复核或撤销权限</summary><p className="approval-muted">仅影响本次研究的这个目标和能力。不会重跑已结束的任务。</p><textarea aria-label={`复核备注 ${target.target.domain}`} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="复核备注（可选）" /><div className="approval-permission-actions"><button className="secondary" disabled={disabled || busy || target.policy_denied} onClick={() => decide("allow_run")}>本次研究允许</button><button className="secondary" disabled={disabled || busy || target.policy_denied} onClick={() => decide("block_run")}>本次研究阻止</button><button className="secondary" disabled={disabled || busy || target.policy_denied} onClick={() => decide("revoke")}>撤销并转人工</button></div>{target.policy_denied && <p className="approval-muted">管理员拒绝规则不能在此覆盖。</p>}</details>{error && <p role="alert" className="approval-error">{error}</p>}</article>;
}

export function ApprovalCenter({ runId, open, onOpenChange, onTask }: { runId: string; open: boolean; onOpenChange: (value: boolean) => void; onTask?: (id: string) => void }) {
  const approvals = useResearchRunStore((s) => s.pendingSecurityApprovals);
  const human = useResearchRunStore((s) => s.pendingHumanAction);
  const sourcePlan = useResearchRunStore(s => s.sourcePlan) ?? human?.payload.source_plan;
  const terminal = useResearchRunStore((s) => s.terminal);
  const lastEvent = useResearchRunStore((s) => s.lastEventId);
  const [tab, setTab] = useState("pending");
  const [filter, setFilter] = useState("all");
  const [selected, setSelected] = useState("");
  const [mobileDetail, setMobileDetail] = useState(true);
  const [snapshot, setSnapshot] = useState<EgressState>();
  const [error, setError] = useState("");
  const [switching, setSwitching] = useState(false);
  const sequence = useRef(0);
  const runRef = useRef(runId);
  const navigatingToTask = useRef(false);
  const returnFocus = useRef<HTMLElement | null>(null);
  const refresh = useCallback(async () => {
    const ticket = ++sequence.current;
    try {
      const result = await researchApi.egressState(runId);
      if (runRef.current === runId && ticket === sequence.current) { setSnapshot(result); setError(""); }
    } catch { if (runRef.current === runId && ticket === sequence.current) setError("暂时无法同步权限状态，请稍后重试。"); }
  }, [runId]);
  useEffect(() => { runRef.current = runId; return () => { sequence.current++; }; }, [runId]);
  useEffect(() => { if (open) void refresh(); }, [open, refresh, lastEvent]);
  useEffect(() => { if (!open || terminal) return; const timer = setInterval(() => void refresh(), 3000); return () => clearInterval(timer); }, [open, terminal, refresh]);
  const groups = new Map<string, SecurityApproval[]>();
  for (const approval of approvals) {
    const key = approval.kind === "network" ? JSON.stringify([approval.target.domain, approval.target.port, approval.capability]) : approval.approval_id;
    groups.set(key, [...(groups.get(key) ?? []), approval]);
  }
  const entries = [...(human ? [{ id: human.action_id, kind: human.type, title: names[human.type], human }] : []), ...Array.from(groups.values()).map((related) => ({ id: related[0].approval_id, kind: related[0].kind, title: `${String(related[0].target.domain ?? related[0].target.tool ?? names[related[0].kind])}${related.length > 1 ? ` · ${related.length} 项` : ""}`, approval: related[0], related }))];
  const visible = entries.filter((entry) => filter === "all" || entry.kind === filter);
  useEffect(() => { if (!visible.some((entry) => entry.id === selected)) setSelected(visible[0]?.id ?? ""); }, [visible, selected]);
  useEffect(() => { if (open) setMobileDetail(visible.length <= 1); }, [open, visible.length]);
  const active = visible.find((entry) => entry.id === selected) ?? visible[0];
  const disabled = terminal || snapshot?.can_resolve !== true;
  function navigateTab(event: React.KeyboardEvent<HTMLDivElement>) {
    const tabs = ["pending", "permissions", "history"];
    const offset = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (!offset && event.key !== "Home" && event.key !== "End") return;
    event.preventDefault();
    const next = event.key === "Home" ? tabs[0] : event.key === "End" ? tabs[2] : tabs[(tabs.indexOf(tab) + offset + tabs.length) % tabs.length];
    setTab(next);
    document.getElementById(`approval-tab-${next}`)?.focus();
  }
  async function switchMode(mode: "manual" | "auto" | "open") {
    setSwitching(true);
    try { await researchApi.switchEgressMode(runId, mode); await refresh(); } catch (cause) { setError(errorMessage(cause)); } finally { setSwitching(false); }
  }
  return <Dialog.Root open={open} onOpenChange={onOpenChange}><Dialog.Portal><Dialog.Overlay className="approval-overlay" /><Dialog.Content className="approval-center" aria-describedby="approval-description"
    onOpenAutoFocus={() => { navigatingToTask.current = false; returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null; }}
    onCloseAutoFocus={(event) => { event.preventDefault(); if (navigatingToTask.current) return; const target = returnFocus.current?.isConnected ? returnFocus.current : document.querySelector<HTMLElement>("[data-approval-trigger]"); target?.focus(); }}>
    <header className="approval-header"><div><span className="approval-kicker">研究工作区</span><Dialog.Title><ShieldCheck size={22} /> 审批中心 <span className="approval-count">{entries.length}</span></Dialog.Title><Dialog.Description id="approval-description">你决定访问范围，研究在授权后继续。</Dialog.Description></div><Dialog.Close className="approval-close" aria-label="关闭审批中心"><X size={20} /></Dialog.Close></header>
    <div className="approval-tabs" role="tablist" aria-label="审批视图" onKeyDown={navigateTab}>{[["pending", "待处理"], ["permissions", "域名权限"], ["history", "记录"]].map(([value, label]) => <button id={`approval-tab-${value}`} aria-controls="approval-tabpanel" role="tab" tabIndex={tab === value ? 0 : -1} aria-selected={tab === value} key={value} onClick={() => setTab(value)}>{label}{value === "pending" && entries.length > 0 && <span>{entries.length}</span>}</button>)}</div>
    <div id="approval-tabpanel" role="tabpanel" aria-labelledby={`approval-tab-${tab}`} data-mobile-detail={mobileDetail} className={`approval-panel ${tab === "pending" ? "approval-pending-panel" : ""}`}>
      {tab === "pending" && <><div className="approval-queue"><label>请求类型<select value={filter} onChange={(e) => setFilter(e.target.value)}><option value="all">全部类型</option>{Array.from(new Set(entries.map((e) => e.kind))).map((kind) => <option key={kind} value={kind}>{names[kind]}</option>)}</select></label>{visible.length > 0 && <div className="approval-queue-list">{visible.map((entry) => <button key={entry.id} aria-current={active?.id === entry.id ? "true" : undefined} onClick={() => { setSelected(entry.id); setMobileDetail(true); }}><span><small>{names[entry.kind]}</small><strong>{entry.title}</strong></span><ChevronRight size={16} /></button>)}</div>}</div>{active && <button className="approval-mobile-back secondary" onClick={() => setMobileDetail(false)}>返回审批列表</button>}{active ? ("approval" in active ? <SecurityApprovalCard key={active.id} runId={runId} approval={active.approval} related={active.related} onTask={onTask ? (id) => { navigatingToTask.current = true; onTask(id); } : undefined} disabled={disabled} onResolved={() => void refresh()} /> : <HumanActionCard key={active.id} runId={runId} action={active.human} disabled={terminal || snapshot?.can_interact === false} onResolved={() => void refresh()} />) : <div className="approval-empty"><CheckCheck size={36} /><h3>暂时没有待处理请求</h3><p>新的请求会出现在这里，不会打断你正在查看的内容。</p></div>}</>}
      {tab === "permissions" && <div className="approval-scroll">{sourcePlan && <SourcePlanDetails action={{ action_id: "source-plan-view", type: "plan_approval", payload: { source_plan: sourcePlan } }} edit={null} onEdit={() => {}} readOnly />}<section className="approval-mode"><h3>未知网站的处理方式</h3><p>当前：{modes[snapshot?.effective_mode as keyof typeof modes] ?? "加载中"}</p><div className="approval-mode-options">{Object.entries(modes).map(([value, label]) => <button className={snapshot?.effective_mode === value ? "primary" : "secondary"} key={value} disabled={disabled || switching || !snapshot?.allowed_modes.includes(value as keyof typeof modes) || snapshot?.effective_mode === value} title={!snapshot?.allowed_modes.includes(value as keyof typeof modes) ? "受管理员基线或研究配置限制" : undefined} onClick={() => switchMode(value as keyof typeof modes)}>{label}</button>)}</div><p className="approval-muted">只能在管理员允许的范围内切换。代理和外部服务的未知目标需单独人工确认。</p><div className="approval-health"><span className="approval-health-dot" data-degraded={!snapshot || !!error || snapshot.health.degraded || snapshot.health.remaining_calls === 0} /><span>{!snapshot ? "正在同步分类服务状态" : error ? "分类服务状态暂不可确认" : snapshot.health.degraded ? "自动分类已降级，转为人工确认" : snapshot?.health.remaining_calls === 0 ? "分类预算已耗尽，转为人工确认" : "自动分类可用"}</span>{snapshot?.health.remaining_calls !== undefined && <small>剩余 {snapshot.health.remaining_calls} 次</small>}</div></section><div className="approval-list-heading"><h3>本次研究的域名权限</h3><span>{snapshot?.targets.length ?? 0} 个目标</span></div>{snapshot?.targets.map((target) => <TargetPermission key={target.target_id} runId={runId} target={target} disabled={disabled} refresh={() => void refresh()} />)}{!snapshot?.targets.length && <p className="approval-muted">访问过的目标及裁决将在这里显示。</p>}</div>}
      {tab === "history" && <div className="approval-scroll"><h3>审批记录</h3>{snapshot?.target_history?.slice().reverse().map((item) => <article className="approval-history" key={`${item.target_id}:${item.version}`}><ShieldCheck size={16} /><div><strong>{item.target.domain}</strong><p>{item.decision && targetDecisions[item.decision]}{item.reason && ` · ${item.reason}`}</p><small>域名权限变更 · {new Date(item.updated_at * 1000).toLocaleString("zh-CN")} · 版本 {item.version}</small></div></article>)}{snapshot?.records.filter((item) => item.status !== "pending").slice().reverse().map((item) => <article className="approval-history" key={item.approval_id}><Check size={16} /><div><strong>{String(item.target.domain ?? item.target.tool ?? names[item.kind])}</strong><p>{item.status === "expired" ? "请求已过期" : item.decision === "allow_once" ? "允许此次操作" : item.decision === "allow_run" ? "本次研究允许" : "拒绝此次操作"}{item.reason && ` · ${item.reason}`}</p><small>{names[item.kind]} · {new Date((item.resolved_at ?? item.requested_at) * 1000).toLocaleString("zh-CN")}</small></div></article>)}{!snapshot?.target_history?.length && !snapshot?.records.some((a) => a.status !== "pending") && <div className="approval-empty"><MessageSquareText size={32} /><p>处理后的安全审批会保留在这里。</p></div>}</div>}
    </div>{error && <div className="approval-sync-error" role="status">{error} <button onClick={() => void refresh()}>重试同步</button></div>}
  </Dialog.Content></Dialog.Portal></Dialog.Root>;
}
