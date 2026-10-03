import { ArrowUpRight, Bot, GitBranch, UserRound } from "lucide-react";
import { Disclosure } from "@/components/ui/insight";
import { StatusBadge } from "@/components/ui/workspace";

type BoardTask = {
  task_id: string; display_title: string; status: string; owner: string | null; admission_status: string;
  blocked_by: string[]; blocks?: string[]; unresolvedBlockedBy?: string[]; phase?: string;
};
type BoardMember = { member_id: string; name: string; purpose: string; status: string; current_task_id?: string };
const admissionLabels: Record<string, string> = { pending: "待交接评估", accepted: "交接已接纳", accepted_with_caveats: "附保留意见接纳", rejected: "交接需补证" };

function lane(task: BoardTask) {
  if (["failed", "cancelled", "timed_out"].includes(task.status)) return "attention";
  if (["awaiting_human", "awaiting_plan_review"].includes(task.phase ?? "") || task.status === "waiting_for_confirmation") return "review";
  if (task.status === "completed") return "completed";
  if (task.status === "pending" || task.unresolvedBlockedBy?.length) return "waiting";
  return "running";
}

export function TeamBoard({ tasks, members, onTask, activityTaskIds }: { tasks: BoardTask[]; members: BoardMember[]; onTask?: (id: string) => void; activityTaskIds: string[] }) {
  const taskName = (id: string) => tasks.find((task) => task.task_id === id)?.display_title || id;
  const canOpen = (id: string) => Boolean(onTask && activityTaskIds.includes(id));
  return <div className="team-workbench">
    <div className="team-member-strip">{members.map((member) => <article className="team-member" key={member.member_id}><span className="agent-card-icon"><UserRound size={17} /></span><div><h3>{member.name}</h3><p>{member.purpose || "研究协调"}</p><StatusBadge status={member.status} /></div>{member.current_task_id && canOpen(member.current_task_id) && <button className="ui-icon" aria-label={`查看${member.name}的当前任务`} onClick={() => onTask?.(member.current_task_id!)}><ArrowUpRight size={16} /></button>}</article>)}</div>
    <div className="team-board">{[{ id: "waiting", title: "等待前置" }, { id: "running", title: "执行与复核" }, { id: "review", title: "等待决定" }, { id: "completed", title: "执行完成" }, { id: "attention", title: "异常与结束" }].map((column) => {
      const items = tasks.filter((task) => lane(task) === column.id);
      return items.length ? <section className="team-lane" key={column.id} aria-label={column.title}><header><h3>{column.title}</h3><span>{items.length}</span></header>{items.map((task) => <article className="team-task" key={task.task_id}>
        <div className="team-task-owner"><Bot size={14} /><span>{members.find((member) => member.member_id === task.owner)?.name || task.owner || "等待领取"}</span></div>
        <h4>{task.display_title || task.task_id}</h4><StatusBadge status={task.status} />
        <span className="team-admission" data-status={task.admission_status}>{admissionLabels[task.admission_status] || task.admission_status || "尚无交接记录"}</span>
        {(task.blocked_by.length > 0 || task.blocks?.length) && <Disclosure title={<span className="activity-card-label"><GitBranch size={14} />任务依赖</span>}>
          {task.blocked_by.length > 0 && <div className="team-dependencies"><small>前置任务</small>{task.blocked_by.map((id) => canOpen(id) ? <button key={id} type="button" className="ui-text-button" onClick={() => onTask?.(id)}>{taskName(id)}<ArrowUpRight size={12} /></button> : <span key={id}>{taskName(id)}</span>)}</div>}
          {!!task.unresolvedBlockedBy?.length && <p>仍在等待：{task.unresolvedBlockedBy.map(taskName).join("、")}</p>}
          {!!task.blocks?.length && <p>下游：{task.blocks.map(taskName).join("、")}</p>}
        </Disclosure>}
        {canOpen(task.task_id) && <button type="button" className="ui-text-button" onClick={() => onTask?.(task.task_id)}>查看执行过程<ArrowUpRight size={13} /></button>}
      </article>)}</section> : null;
    })}</div>
  </div>;
}
