import { ArrowUpRight, Bot, CircleDot, Clock3, Layers } from "lucide-react";
import { memo } from "react";
import { StatusBadge } from "@/components/ui/workspace";
import type { ResearchTask } from "@/lib/types";
import { activityPhaseNames } from "../activity/presentation";

export const AgentTaskCard = memo(function AgentTaskCard({ task, selected, onOpen }: { task: ResearchTask; selected: boolean; onOpen: () => void }) {
  return <button type="button" data-task-id={task.task_id} className={`agent-task-card ${selected ? "selected" : ""}`} aria-haspopup="dialog" aria-expanded={selected} onClick={onOpen}>
    <span className="agent-card-top"><span className="agent-card-icon"><Bot size={19} aria-hidden /></span><StatusBadge status={task.status ?? "pending"} /></span>
    <strong>{task.title || task.task_id}</strong>
    <span className="agent-card-activity"><CircleDot size={13} aria-hidden />{task.activity_label || activityPhaseNames[task.activity_phase ?? task.phase ?? ""] || "等待活动更新"}</span>
    <span className="agent-card-footer"><span><Layers size={13} aria-hidden />{task.source_count == null ? "来源数未提供" : `${task.source_count} 个来源`}</span><span>{task.iteration != null ? `第 ${task.iteration} 轮` : task.elapsed_ms != null ? <><Clock3 size={12} aria-hidden />{Math.round(task.elapsed_ms / 1000)} 秒</> : "查看过程"}<ArrowUpRight size={13} aria-hidden /></span></span>
  </button>;
});
