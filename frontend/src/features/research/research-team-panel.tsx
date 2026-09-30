"use client";

import { useEffect, useState } from "react";
import { EmptyState, SearchSelect, Tabs } from "@/components/ui/workspace";
import { apiFetch } from "@/lib/api";

type Team = {
  enabled: boolean;
  name?: string;
  status?: string;
  mode?: string;
  execution_mode?: string;
  metrics?: { claim_conflicts: number; message_backlog: number; member_recoveries: number; plan_review_seconds: number | null };
  members: { member_id: string; name: string; purpose: string; status: string; execution_mode?: string; mode_override?: boolean; current_task_id?: string }[];
  tasks: { task_id: string; version: number; display_title: string; status: string; owner: string | null; admission_status: string; admission_reason?: string; termination?: string; blocked_by: string[]; blocks?: string[]; unresolvedBlockedBy?: string[]; phase?: string; execution_mode?: string; handoff_assessment?: { reason: string; caveats?: string[] } }[];
  messages: { event_id: string; sender: string; recipients: string[]; delivery_status?: string; payload: { message?: string | Record<string, unknown>; content?: string } }[];
  plans?: { task_id: string; version: number; owner: string; content: Record<string, unknown>; status: string; feedback: string; reviewed_by?: string }[];
  proposals?: { event_id: string; member_id: string; status: string; task_id?: string; content: { subject: string; description: string; rejection_reason?: string } }[];
};

export function ResearchTeamPanel({ runId, revision, terminal }: { runId: string; revision: number; terminal: boolean }) {
  const [tab, setTab] = useState("members");
  const [team, setTeam] = useState<Team>();
  const [error, setError] = useState("");
  const [recipient, setRecipient] = useState("*");
  const [message, setMessage] = useState("");
  const [sending, setSending] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [planFeedback, setPlanFeedback] = useState<Record<string, string>>({});
  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => {
      void apiFetch<Team>(`/runs/${encodeURIComponent(runId)}/team`, { signal: controller.signal })
        .then((value) => { setTeam(value); setError(""); })
        .catch((reason) => { if (!controller.signal.aborted) setError(String(reason)); });
    }, 200);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [runId, revision, refresh]);
  useEffect(() => {
    if (terminal) return;
    const timer = setInterval(() => setRefresh((value) => value + 1), 3000);
    return () => clearInterval(timer);
  }, [runId, terminal]);
  if (team?.enabled === false) return <EmptyState title="本次研究未启用团队协作" description="请在研究进展中查看执行任务。" />;
  const name = (id: string | null) => team?.members?.find((member) => member.member_id === id)?.name || id || "未领取";
  const labels: Record<string, string> = { pending: "等待中", running: "执行中", waiting_for_confirmation: "等待确认", completed: "已完成", failed: "失败", cancelled: "已取消", timed_out: "已超时", accepted: "已接纳", accepted_with_caveats: "附保留意见", rejected: "需补证", active: "协作中", closed: "已关闭" };
  Object.assign(labels, { direct: "直接执行", plan_approval: "先规划再审批", planning: "规划中", awaiting_plan_review: "等待 Lead 审核", awaiting_human: "需要人工介入", executing: "研究执行中", assessing: "质量评估", idle: "空闲", stopping: "正在退出", approved: "已批准", superseded: "已失效", delivered: "已送达", applied: "已应用" });
  const cell = { padding: "10px 12px", verticalAlign: "top", borderBottom: "1px solid var(--border, #e5e5e5)" };
  async function send() {
    if (!message.trim()) return;
    setSending(true);
    try {
      await apiFetch(`/runs/${encodeURIComponent(runId)}/team/messages`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ to: recipient, message: message.trim(), command_id: crypto.randomUUID() }) });
      setMessage(""); setRefresh((value) => value + 1);
    } catch (reason) { setError(String(reason)); }
    finally { setSending(false); }
  }
  async function revise(task: Team["tasks"][number]) {
    setSending(true);
    try {
      await apiFetch(`/runs/${encodeURIComponent(runId)}/team/tasks/${encodeURIComponent(task.task_id)}/plan-revision`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ version: task.version, feedback: planFeedback[task.task_id], command_id: crypto.randomUUID() }),
      });
      setRefresh((value) => value + 1);
    } catch (reason) { setError(String(reason)); }
    finally { setSending(false); }
  }
  return <section className="panel" aria-label="研究团队">
    <div className="panel-header"><h2>{team?.name || "研究团队"}</h2><span>{team?.mode === "teams" ? "Agent Teams" : "Collaborator"} · {terminal ? "已结束" : labels[team?.status || ""] || "等待 Lead 创建"}</span></div>
    <div className="panel-body" style={{ display: "grid", gap: 16 }}>
      {error && <p role="alert">团队状态暂不可用：{error}</p>}
      {team?.mode === "teams" && team.metrics && <p>领取冲突 {team.metrics.claim_conflicts} 次 · 消息积压 {team.metrics.message_backlog} 条 · 成员恢复 {team.metrics.member_recoveries} 次 · 平均计划审核 {team.metrics.plan_review_seconds == null ? "暂无" : `${Number(team.metrics.plan_review_seconds).toFixed(1)} 秒`}</p>}
      <Tabs label="团队详情" value={tab} onChange={setTab} items={[{ value: "members", label: "成员与任务" }, { value: "plans", label: "计划" }, { value: "quality", label: "质量记录" }, { value: "messages", label: "消息" }]}>
        {!team && !error && <EmptyState title="正在读取团队状态" />}
        {tab === "members" && <>      <div className="task-grid">{team?.members?.map((member) => <div className="task-card" key={member.member_id}>
        <h3>{member.name}</h3><p>{member.purpose || "团队协调者"}</p>
        {member.member_id !== "lead" && <p>{labels[member.execution_mode || "direct"]} · {member.mode_override ? "成员单独设置" : "继承团队默认"}</p>}
        <small>{labels[team.tasks?.find((task) => task.owner === member.member_id && ["running", "waiting_for_confirmation"].includes(task.status))?.phase || member.status] || member.status}</small>
      </div>)}</div>
      {!!team?.tasks?.length && <div style={{ overflowX: "auto" }}><table style={{ width: "100%", minWidth: 640, textAlign: "left", borderCollapse: "collapse", fontSize: 14 }}><thead><tr><th style={cell}>任务</th><th style={cell}>负责人</th><th style={cell}>状态</th><th style={cell}>证据准入</th><th style={cell}>依赖</th></tr></thead><tbody>
        {team.tasks.map((task) => <tr key={task.task_id}><td style={cell}>{task.display_title || task.task_id}</td><td style={cell}>{name(task.owner)}</td><td style={cell}>{labels[task.status] || task.status}{task.phase && <small><br />{labels[task.phase] || task.phase}</small>}</td><td style={cell}>{labels[task.admission_status] || task.admission_status}</td><td style={cell}>前置：{task.blocked_by.map((id) => team.tasks.find((item) => item.task_id === id)?.display_title || id).join("、") || "无"}<br />仍阻塞：{task.unresolvedBlockedBy?.join("、") || "无"}<br />阻塞下游：{task.blocks?.join("、") || "无"}</td></tr>)}
      </tbody></table></div>}
</>}
        {tab === "plans" && <>      {!terminal && team?.tasks.filter((task) => task.phase === "awaiting_human").map((task) => <form key={task.task_id} onSubmit={(event) => { event.preventDefault(); void revise(task); }}>
        <p>{task.display_title}：计划已达到自动修订上限。请补充指导；成员重新规划后仍须 Lead 审核。</p>
        <textarea aria-label={`计划修订指导 ${task.task_id}`} value={planFeedback[task.task_id] || ""} maxLength={12000}
          onChange={(event) => setPlanFeedback((value) => ({ ...value, [task.task_id]: event.target.value }))} />
        <button disabled={sending || !planFeedback[task.task_id]?.trim()}>允许继续修订计划</button>
      </form>)}
      {!!team?.plans?.length && <section className="team-records"><h3>成员计划与 Lead 审核</h3>{team.plans.map((plan) => <article key={`${plan.task_id}:${plan.version}`}>
        <h3>{plan.task_id} · 计划 v{plan.version} · {labels[plan.status] || plan.status}</h3>
        <p>成员：{name(plan.owner)}；审核：{plan.reviewed_by || "待 Lead 审核"}</p>
        <pre style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify(plan.content, null, 2)}</pre>
        {plan.feedback && <p>审核意见：{plan.feedback}</p>}
      </article>)}</section>}
{!team?.plans?.length && <EmptyState title="尚无成员计划" description="计划与审核记录会显示在这里。" />}</>}
        {tab === "quality" && <>      <section className="team-records"><h3>质量评估与补证记录</h3>
        {team?.tasks.filter((task) => task.handoff_assessment || task.admission_reason).map((task) => <article key={task.task_id}>
          <h3>{task.display_title} · {labels[task.admission_status] || task.admission_status}</h3>
          {task.admission_reason && <p>任务准入原因：{task.admission_reason === "research_execution_not_completed: exceed_max_iters" ? "研究达到迭代上限，未正常完成；质量评分通过也不会自动解锁下游。" : task.admission_reason === "quality_evaluator_unavailable" ? "质量评估不可用，结果尚未准入。" : task.admission_reason}</p>}
          <p>{task.handoff_assessment?.reason}</p>
          {!!task.handoff_assessment?.caveats?.length && <p>保留意见：{task.handoff_assessment.caveats.join("；")}</p>}
        </article>)}
        {team?.proposals?.map((proposal) => <article key={proposal.event_id}>
          <h3>{proposal.content.subject} · {labels[proposal.status] || proposal.status}</h3>
          <p>{name(proposal.member_id)}：{proposal.content.description}</p>
          {proposal.task_id && <p>已发布任务：{proposal.task_id}</p>}
          {proposal.content.rejection_reason && <p>Lead 意见：{proposal.content.rejection_reason}</p>}
        </article>)}
      </section>
</>}
        {tab === "messages" && <>      <section className="team-records"><h3>团队消息（最近 {team?.messages?.length || 0} 条）</h3>{team?.messages?.map((item) => <p key={item.event_id}><b>{name(item.sender)} → {item.recipients.map(name).join("、")}</b> · {item.delivery_status === "accepted" ? "已受理" : labels[item.delivery_status || ""] || "已记录"}<br />{typeof item.payload.message === "object" ? JSON.stringify(item.payload.message) : item.payload.message ?? item.payload.content}</p>)}</section>
      {!terminal && team?.status === "active" && <form onSubmit={(event) => { event.preventDefault(); void send(); }} style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
        <SearchSelect label="消息接收成员" value={recipient} onChange={setRecipient} options={[{ value: "*", label: "全体成员" }, ...team.members.filter((member) => member.member_id !== "lead" && member.status !== "closed").map((member) => ({ value: member.member_id, label: member.name }))]} />
        <input aria-label="团队消息" placeholder="发送研究方向或补充信息" value={message} maxLength={12000} onChange={(event) => setMessage(event.target.value)} style={{ flex: 1 }} />
        <button className="secondary" disabled={sending || !message.trim()}>发送消息</button>
      </form>}
</>}
      </Tabs>
    </div>
  </section>;
}
