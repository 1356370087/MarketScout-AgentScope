"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { AlertTriangle, Database, LoaderCircle, RotateCcw, X } from "lucide-react";
import { StatusBadge } from "@/components/ui/workspace";
import { MetricStrip } from "@/components/ui/insight";
import { ActivityCard } from "./activity/activity-cards";
import { groupActivity } from "./activity/presentation";
import "./research-ui.css";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTaskActivity } from "@/hooks/use-task-activity";
import type { ResearchSource, ResearchTask, TaskActivityKind, TaskActivityPhase } from "@/lib/types";

const filters: Array<{ value?: TaskActivityKind; label: string }> = [
  { label: "全部" }, { value: "model", label: "模型" }, { value: "tool", label: "工具" },
  { value: "source", label: "来源" }, { value: "quality", label: "质量" },
  { value: "security", label: "安全" }, { value: "error", label: "错误" },
];
const phaseNames: Record<TaskActivityPhase, string> = {
  queued: "排队", initializing: "准备", reasoning: "模型规划", tool_execution: "工具执行",
  evidence_review: "证据评估", quality_check: "质量复核", gap_recovery: "补证恢复",
  compressing: "压缩", handoff: "交接", terminal: "终态",
};
const phaseRail: TaskActivityPhase[] = ["initializing", "reasoning", "tool_execution", "evidence_review", "quality_check", "gap_recovery", "compressing", "handoff"];

type DrawerProps = { runId: string; task?: ResearchTask; onClose: () => void; onSource?: (source: ResearchSource) => void };

export function TaskActivityDrawer(props: DrawerProps) {
  return props.task ? <TaskActivityPanel key={`${props.runId}:${props.task.task_id}`} {...props} /> : null;
}

function TaskActivityPanel({ runId, task, onClose, onSource }: DrawerProps) {
  const navigatingToSource = useRef(false);
  const [kind, setKind] = useState<TaskActivityKind | undefined>();
  const { events, connection, loading, error, source, detailLevel, hasMore, loadOlder } = useTaskActivity(runId, task?.task_id ?? "");
  const viewportRef = useRef<HTMLDivElement>(null);
  const [following, setFollowing] = useState(true);
  const [unread, setUnread] = useState(0);
  const current = events.at(-1);
  const lastSequence = useRef(0);
  const [olderLoading, setOlderLoading] = useState(false);
  const [olderError, setOlderError] = useState("");

  useEffect(() => {
    const added = events.filter((event) => event.sequence > lastSequence.current).length;
    lastSequence.current = events.at(-1)?.sequence ?? 0;
    if (!added) return;
    if (following) {
      const frame = requestAnimationFrame(() => viewportRef.current?.scrollTo({ top: viewportRef.current.scrollHeight, behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth" }));
      return () => cancelAnimationFrame(frame);
    }
    setUnread((count) => count + added);
  }, [events, following]);

  async function readOlder() {
    if (olderLoading) return;
    setFollowing(false); setOlderLoading(true); setOlderError("");
    try { await loadOlder(); } catch { setOlderError("较早的事件读取失败，请重试。"); } finally { setOlderLoading(false); }
  }

  const phasesSeen = useMemo(() => new Set(events.map((event) => event.phase)), [events]);
  const visibleEvents = useMemo(() => kind ? events.filter((event) => event.kind === kind) : events, [events, kind]);
  const timelineItems = useMemo(() => groupActivity(visibleEvents), [visibleEvents]);
  if (!task) return null;
  return <Dialog.Root open onOpenChange={(open) => { if (!open) onClose(); }}>
    <Dialog.Portal>
      <Dialog.Overlay className="activity-overlay" />
      <Dialog.Content className="activity-drawer" aria-describedby="task-activity-description" onCloseAutoFocus={(event) => {
        event.preventDefault();
        if (navigatingToSource.current) return;
        requestAnimationFrame(() => {
          const taskCard = document.querySelector<HTMLButtonElement>(`[data-task-id="${CSS.escape(task.task_id)}"]`);
          (taskCard ?? document.querySelector<HTMLButtonElement>("[data-task-focus-fallback]"))?.focus();
        });
      }}>
        <header className="activity-header">
          <div><span className="eyebrow mono">SUBAGENT / {task.task_id}</span><Dialog.Title>{task.title ?? task.task_id}</Dialog.Title><Dialog.Description id="task-activity-description">真实执行事件与安全业务摘要</Dialog.Description></div>
          <Dialog.Close className="activity-close" aria-label="关闭任务详情"><X size={18} /></Dialog.Close>
        </header>
        <section className="activity-summary">
          <div className="activity-summary-row"><StatusBadge status={task.status ?? "pending"} /><span className={`activity-connection ${connection}`}><i />{{ connecting: "连接中", connected: "实时连接", reconnecting: "正在重连", closed: "记录已同步", error: "连接异常" }[connection]}</span><span className="activity-origin">{source === "native" ? "原生执行记录" : source === "derived_trace" ? "历史记录推导" : "仅有摘要"}</span></div>
          <MetricStrip label="任务调用统计" items={[
            { label: "模型调用", value: task.model_call_count },
            { label: "工具调用", value: task.tool_call_count },
            { label: "来源", value: task.source_count },
            { label: "警告 / 重试", value: `${task.warning_count ?? "—"} / ${task.retry_count ?? "—"}` },
          ]} />
        </section>
        <section className="activity-live" data-status={current?.status ?? "pending"}>
          <div><span>当前活动</span><b>{current ? phaseNames[current.phase] : task.activity_label ?? "等待活动事件"}</b></div>
          <p>{connection === "reconnecting" ? "连接中断，正在保留最后已知活动并重连。" : current?.summary ?? "任务存在，但暂无可公开的细粒度事件。"}</p>
          {connection === "reconnecting" && <RotateCcw className="activity-spin" size={18} />}
        </section>
        <section className="activity-phase-rail" aria-label="研究循环阶段">
          {phaseRail.map((phase) => <span key={phase} data-active={current?.phase === phase} data-seen={phasesSeen.has(phase)}>{phaseNames[phase]}</span>)}
        </section>
        <nav className="activity-filters" aria-label="事件筛选">
          {filters.map((filter) => <button key={filter.label} className={filter.value === kind ? "active" : ""} onClick={() => setKind(filter.value)}>{filter.label}</button>)}
          <span>{detailLevel === "preview" ? "含安全预览" : "公开摘要"}</span>
        </nav>
        <div className="activity-timeline" ref={viewportRef} onScroll={(event) => {
          const element = event.currentTarget;
          const atBottom = element.scrollHeight - element.scrollTop - element.clientHeight < 80;
          setFollowing(atBottom);
          if (atBottom) setUnread(0);
        }}>
          {hasMore && <button className="activity-load-more" disabled={olderLoading} onClick={() => void readOlder()}>{olderLoading ? "读取中…" : "加载更早事件"}</button>}
          {olderError && <p role="alert" className="form-alert error">{olderError}</p>}
          {loading && <div className="activity-empty"><LoaderCircle className="activity-spin" />正在读取任务事件……</div>}
          {error && <div className="activity-empty error"><AlertTriangle />任务活动暂时不可用</div>}
          {!loading && !error && visibleEvents.length === 0 && <div className="activity-empty"><Database />{events.length ? "当前筛选没有匹配事件。" : "此任务仅有摘要状态，未发现可安全关联的历史事件。"}</div>}
          {timelineItems.map((item, index) => {
            const event = item.events[0];
            const previous = timelineItems[index - 1]?.events.at(-1);
            return <div className="activity-iteration" key={event.event_id}>
              {(index === 0 || previous?.iteration !== event.iteration) && event.iteration != null && <div className="activity-iteration-label">第 {event.iteration} 轮</div>}
              {<ActivityCard group={item} onSource={onSource ? (source) => { navigatingToSource.current = true; onSource(source); } : undefined} />}
            </div>;
          })}
        </div>
        {!following && <button className="activity-unread" onClick={() => { setFollowing(true); setUnread(0); viewportRef.current?.scrollTo({ top: viewportRef.current.scrollHeight, behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth" }); }}>{unread ? `${unread} 条新事件` : "回到最新"}</button>}
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>;
}
