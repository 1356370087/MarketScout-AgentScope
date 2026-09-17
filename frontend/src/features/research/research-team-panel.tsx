"use client";

import { useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";

type Team = {
  enabled: boolean;
  name?: string;
  status?: string;
  members: { member_id: string; name: string; purpose: string; status: string }[];
  tasks: { task_id: string; display_title: string; status: string; owner: string | null; admission_status: string; blocked_by: string[] }[];
  messages: { event_id: string; sender: string; recipients: string[]; payload: { message: string } }[];
};

export function ResearchTeamPanel({ runId, revision, terminal }: { runId: string; revision: number; terminal: boolean }) {
  const [team, setTeam] = useState<Team>();
  const [error, setError] = useState("");
  const [recipient, setRecipient] = useState("*");
  const [message, setMessage] = useState("");
  const [sending, setSending] = useState(false);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => {
      void apiFetch<Team>(`/runs/${encodeURIComponent(runId)}/team`, { signal: controller.signal })
        .then((value) => { setTeam(value); setError(""); })
        .catch((reason) => { if (!controller.signal.aborted) setError(String(reason)); });
    }, 200);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [runId, revision, refresh]);
  if (team?.enabled === false) return null;
  const name = (id: string | null) => team?.members?.find((member) => member.member_id === id)?.name || id || "未领取";
  const labels: Record<string, string> = { pending: "等待中", running: "执行中", waiting_for_confirmation: "等待确认", completed: "已完成", failed: "失败", cancelled: "已取消", timed_out: "已超时", accepted: "已接纳", accepted_with_caveats: "附保留意见", rejected: "需补证", active: "协作中", closed: "已关闭" };
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
  return <section className="panel" aria-label="研究团队">
    <div className="panel-header"><h2>{team?.name || "研究团队"}</h2><span>{terminal ? "已结束" : labels[team?.status || ""] || "等待创建"}</span></div>
    <div className="panel-body" style={{ display: "grid", gap: 16 }}>
      {error && <p role="alert">团队状态暂不可用：{error}</p>}
      <div className="task-grid">{team?.members?.map((member) => <div className="task-card" key={member.member_id}>
        <h3>{member.name}</h3><p>{member.purpose || "团队协调者"}</p>
        <small>{team.tasks?.some((task) => task.owner === member.member_id && ["running", "waiting_for_confirmation"].includes(task.status)) ? "正在研究" : member.status === "closed" ? "已关闭" : "空闲"}</small>
      </div>)}</div>
      {!!team?.tasks?.length && <div style={{ overflowX: "auto" }}><table style={{ width: "100%", minWidth: 640, textAlign: "left", borderCollapse: "collapse", fontSize: 14 }}><thead><tr><th style={cell}>任务</th><th style={cell}>负责人</th><th style={cell}>状态</th><th style={cell}>证据准入</th><th style={cell}>依赖</th></tr></thead><tbody>
        {team.tasks.map((task) => <tr key={task.task_id}><td style={cell}>{task.display_title || task.task_id}</td><td style={cell}>{name(task.owner)}</td><td style={cell}>{labels[task.status] || task.status}</td><td style={cell}>{labels[task.admission_status] || task.admission_status}</td><td style={cell}>{task.blocked_by.map((id) => team.tasks.find((item) => item.task_id === id)?.display_title || id).join("、") || "无"}</td></tr>)}
      </tbody></table></div>}
      <details><summary>团队消息（最近 {team?.messages?.length || 0} 条）</summary>{team?.messages?.map((item) => <p key={item.event_id}><b>{name(item.sender)} → {item.recipients.map(name).join("、")}</b><br />{item.payload.message}</p>)}</details>
      {!terminal && team?.status === "active" && <form onSubmit={(event) => { event.preventDefault(); void send(); }} style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
        <label>发送给 <select aria-label="消息接收成员" value={recipient} onChange={(event) => setRecipient(event.target.value)}><option value="*">全体成员</option>{team.members.filter((member) => member.member_id !== "lead" && member.status !== "closed").map((member) => <option key={member.member_id} value={member.member_id}>{member.name}</option>)}</select></label>
        <input aria-label="团队消息" placeholder="发送研究方向或补充信息" value={message} maxLength={12000} onChange={(event) => setMessage(event.target.value)} style={{ flex: 1 }} />
        <button className="secondary" disabled={sending || !message.trim()}>发送消息</button>
      </form>}
    </div>
  </section>;
}
