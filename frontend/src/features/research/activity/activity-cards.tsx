import { Bot, CheckCircle2, Clock3, Search, ShieldAlert, Wrench } from "lucide-react";
import { Disclosure, StructuredContent } from "@/components/ui/insight";
import type { ResearchSource, TaskActivityEvent } from "@/lib/types";
import { SourceCard } from "../evidence/source-card";
import { activitySource, type ActivityGroup } from "./presentation";

const statusNames: Record<string, string> = { pending: "等待", running: "执行中", success: "已完成", warning: "需关注", error: "失败", cancelled: "已取消" };
const kindNames: Record<string, string> = { model: "模型", tool: "工具", source: "来源", quality: "质量", security: "安全", error: "错误", control: "研究指导", checkpoint: "恢复记录", lifecycle: "任务状态" };
const decisionNames: Record<string, string> = { pass: "通过", fail: "未通过", accepted: "已接纳", accepted_with_caveats: "附保留意见接纳", rejected: "需补证", revise: "需要修订" };

function EventHeader({ event, icon }: { event: TaskActivityEvent; icon: React.ReactNode }) {
  return <header className="activity-card-heading"><span className="activity-card-icon" aria-hidden>{icon}</span><div><small>{kindNames[event.kind] ?? event.kind}</small><h4>{event.title}</h4></div><span className="activity-card-status" data-status={event.status}>{statusNames[event.status] ?? event.status}</span></header>;
}

export function ActivityCard({ group, onSource }: { group: ActivityGroup; onSource?: (source: ResearchSource) => void }) {
  const first = group.events[0], latest = group.events.at(-1)!;
  if (group.type === "sources") return <Disclosure title={<span className="activity-card-label"><Search size={16} />发现 {group.events.length} 个来源</span>} meta="本任务的采集记录" open>
    <div className="activity-source-cards">{group.events.map((event) => { const source = activitySource(event); return source ? <SourceCard key={event.event_id} source={source} onSelect={onSource} /> : <p key={event.event_id}>{event.summary || event.title}</p>; })}</div>
  </Disclosure>;
  if (group.type === "tool") return <article className="activity-rich-card" data-status={latest.status}>
    <EventHeader event={latest} icon={<Wrench size={17} />} />
    {group.events.some((event) => event.type === "tool.progress") && <ol className="web-progress-list" aria-label="搜索与抓取进度">
      {group.events.filter((event) => event.type === "tool.progress").map((event) => <li key={event.event_id} data-status={event.status}>
        <strong>{typeof event.payload.provider === "string" ? `${event.payload.provider} · ` : ""}{event.title}</strong>
        <span>{event.summary}</span>
        {typeof event.payload.result_count === "number" && <small>{event.payload.result_count} 个候选来源</small>}
        {typeof event.payload.raw_result_count === "number" && <small>返回 {event.payload.raw_result_count} 条 · 范围过滤 {String(event.payload.filtered_result_count ?? 0)} 条 · 去重后 {String(event.payload.unique_result_count ?? event.payload.result_count ?? 0)} 条</small>}
        {event.payload.result_status === "all_filtered" && <small>本渠道返回结果均不在允许的来源范围内</small>}
        {typeof event.payload.provider_requests === "number" && event.payload.provider_requests > 1 && <small>含一次简化查询，共 {event.payload.provider_requests} 次请求</small>}
        {typeof event.payload.backend === "string" && <small>读取方式：{event.payload.backend}</small>}
        {event.payload.metrics != null && <Disclosure title="对照评估结果"><StructuredContent value={event.payload.metrics} /></Disclosure>}
      </li>)}
    </ol>}
    {typeof first.payload.args_summary === "string" && <div className="activity-query"><Search size={15} aria-hidden /><span>{first.payload.args_summary}</span></div>}
    <p>{latest.summary}</p><div className="activity-card-facts">
      {typeof latest.payload.tool_name === "string" && <span>{latest.payload.tool_name}</span>}
      {typeof latest.payload.rerank_completed === "boolean" && <span>{latest.payload.rerank_completed ? "语义重排已完成" : "未完成重排"}</span>}
      {typeof latest.payload.retrieval_profile === "string" && <span>配置 {latest.payload.retrieval_profile}</span>}
      {latest.duration_ms != null && <span><Clock3 size={12} />{(latest.duration_ms / 1000).toFixed(2)} 秒</span>}
      {typeof latest.payload.execution_ms === "number" && <span>执行 {(latest.payload.execution_ms / 1000).toFixed(2)} 秒</span>}
      {typeof latest.payload.queue_ms === "number" && <span>排队 {(latest.payload.queue_ms / 1000).toFixed(2)} 秒</span>}
      {typeof latest.payload.approval_wait_ms === "number" && <span>审批等待 {(latest.payload.approval_wait_ms / 1000).toFixed(2)} 秒</span>}
      {typeof latest.payload.source_count === "number" && <span>{latest.payload.source_count} 个返回来源</span>}
      {typeof latest.payload.retry_count === "number" && latest.payload.retry_count > 0 && <span>重试 {latest.payload.retry_count} 次</span>}
      <time>{new Date(latest.timestamp).toLocaleTimeString("zh-CN", { hour12: false })}</time>
    </div>
    {latest.payload.error_code != null && <p className="activity-card-error">{String(latest.payload.error_code)}</p>}
    {Array.isArray(latest.payload.urls) && latest.payload.urls.length > 0 && <Disclosure title="工具返回的来源地址"><div className="activity-source-cards">{latest.payload.urls.filter((url): url is string => typeof url === "string").map((url) => <SourceCard key={url} source={{ source_id: url, url, task_id: latest.task_id }} onSelect={onSource} />)}</div></Disclosure>}
  </article>;
  const quality = latest.kind === "quality";
  return <article className="activity-rich-card" data-status={latest.status}>
    <EventHeader event={latest} icon={quality ? <CheckCircle2 size={17} /> : latest.kind === "model" ? <Bot size={17} /> : <ShieldAlert size={17} />} />
    <p>{latest.summary}</p>
    {quality && <div className="activity-card-facts">{["decision", "admission_status"].map((key) => typeof latest.payload[key] === "string" ? <span key={key}>{key === "admission_status" ? "交接：" : "评估："}{decisionNames[latest.payload[key] as string] ?? String(latest.payload[key])}</span> : null)}</div>}
    {quality && ["hard_rejection_reasons", "missing_information", "follow_up_tasks"].map((key) => Array.isArray(latest.payload[key]) && (latest.payload[key] as unknown[]).length > 0 ? <StructuredContent key={key} value={{ [key]: latest.payload[key] }} /> : null)}
    <div className="activity-card-facts">{typeof latest.payload.model === "string" && <span>{latest.payload.model}</span>}{latest.duration_ms != null && <span>{(latest.duration_ms / 1000).toFixed(2)} 秒</span>}<time>{new Date(latest.timestamp).toLocaleTimeString("zh-CN", { hour12: false })}</time></div>
    {latest.payload.preview !== undefined && <Disclosure title="安全诊断预览"><StructuredContent value={latest.payload.preview} /></Disclosure>}
  </article>;
}
