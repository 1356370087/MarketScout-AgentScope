"use client";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { platformFetch } from "@/lib/platform";
import { Heading, Alert, PlatformModal, WorkerState } from "./ui";
type Item = { id: string; owner_id: string; status: string; error_code?: string; kind?: string; run_id?: string; retryable?: boolean };
type Operations = { runs: Item[]; documents: Item[]; knowledge_jobs: Item[]; publications: Item[]; workers: Record<string, unknown> };
export default function OperationsPage() {
  const cache = useQueryClient();
  const query = useQuery({ queryKey: ["platform", "operations"], queryFn: () => platformFetch<Operations>("/operations"), refetchInterval: 10000 });
  const [selected, setSelected] = useState<{ action: string; resource_id: string; label: string } | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  async function submit() { setBusy(true); try { await platformFetch("/operations", "POST", { action: selected?.action, resource_id: selected?.resource_id, reason }); setSelected(null); setReason(""); await cache.invalidateQueries({ queryKey: ["platform"] }); } catch (cause) { setError(cause); } finally { setBusy(false); } }
  return <><Heading title="任务与 Worker" description="查看运行元数据，处理需要维护的任务。" /><Alert error={error || query.error} />
    <section className="panel platform-panel"><h2>后台服务状态</h2>{Object.entries(query.data?.workers ?? {}).map(([name, value]) => <WorkerState key={name} name={name === "documents" ? "文档摄取" : name === "knowledge" ? "知识库维护" : "报告发布"} value={value} />)}</section>
    {([ ["runs", "研究运行"], ["documents", "文档任务"], ["knowledge_jobs", "知识库维护"], ["publications", "报告发布"] ] as const).map(([key, label]) => <section className="panel platform-panel" key={key}><h2>{label}</h2><div className="platform-table-wrap"><table className="platform-table"><thead><tr><th>任务编号</th><th>所属用户</th><th>状态</th><th>错误分类</th><th>维护操作</th></tr></thead><tbody>{query.data?.[key].map((item) => <tr key={item.id}><td>{item.id}{item.kind && <small>{item.kind}</small>}</td><td>{item.owner_id}</td><td><span className="platform-state">{item.status}</span></td><td>{item.error_code || "—"}</td><td><div className="platform-actions">{key === "runs" && !["completed", "cancelled", "failed"].includes(item.status) && <button className="danger" onClick={() => setSelected({ action: "cancel_run", resource_id: item.id, label: "取消研究" })}>取消研究</button>}{key === "documents" && <><button className="secondary" onClick={() => setSelected({ action: "reindex_document", resource_id: item.id, label: "重建文档索引" })}>重索引</button>{item.status === "failed" && <button className="secondary" onClick={() => setSelected({ action: "retry_document", resource_id: item.id, label: "重试文档处理" })}>重试</button>}</>}{key === "knowledge_jobs" && item.status === "failed" && <button className="secondary" onClick={() => setSelected({ action: "knowledge_maintenance", resource_id: item.id, label: "重试知识库任务" })}>重试</button>}{key === "publications" && item.status === "failed" && item.retryable && <button className="secondary" onClick={() => setSelected({ action: "retry_publication", resource_id: `${item.run_id}/${item.id}`, label: "重试报告发布" })}>重试</button>}</div></td></tr>)}</tbody></table>{!query.data?.[key].length && <div className="platform-empty">暂无任务记录</div>}</div></section>)}
    {selected && <PlatformModal title={selected.label} onClose={() => { if (!busy) setSelected(null); }}><p>任务编号：{selected.resource_id}</p><div className="platform-form"><label>维护原因<textarea value={reason} onChange={(event) => setReason(event.target.value)} /></label><Alert error={error} /><div className="platform-actions"><button className="primary" disabled={busy || !reason.trim()} onClick={() => void submit()}>确认执行</button><button className="secondary" onClick={() => setSelected(null)}>返回</button></div></div></PlatformModal>}
  </>;
}
