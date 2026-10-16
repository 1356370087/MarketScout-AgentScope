"use client";
import { useQuery } from "@tanstack/react-query";
import { platformFetch } from "@/lib/platform";
import { Heading, Alert } from "./ui";
type Event = { id: string; actor_id: string; action: string; subject_id: string; detail: Record<string, unknown>; created_at: string };
export default function AuditPage() {
  const query = useQuery({ queryKey: ["platform", "audit"], queryFn: () => platformFetch<{ items: Event[] }>("/audit") });
  return <><Heading title="平台审计" description="追踪配置发布、凭据变更、配额调整与维护操作。" /><Alert error={query.error} /><section className="panel platform-panel"><div className="platform-table-wrap"><table className="platform-table"><thead><tr><th>时间</th><th>操作者</th><th>操作</th><th>对象</th><th>详情</th></tr></thead><tbody>{query.data?.items.map((event) => <tr key={event.id}><td>{new Date(event.created_at).toLocaleString("zh-CN")}</td><td>{event.actor_id}</td><td>{event.action}</td><td>{event.subject_id}</td><td><details><summary>查看记录</summary><pre>{JSON.stringify(event.detail, null, 2)}</pre></details></td></tr>)}</tbody></table>{query.isSuccess && !query.data.items.length && <div className="platform-empty">暂无审计记录</div>}</div></section></>;
}
