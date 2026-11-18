"use client";

import "./review-versions.css";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { researchApi } from "@/lib/api";

const childStatuses: Record<string, string> = { ready: "准备中", running: "复核中", waiting: "等待审批",
  completed: "已结束", failed: "执行失败", cancelled: "已取消" };
const money = (value: number) => (value / 1_000_000).toFixed(6) + " 美元";

export function ReviewVersions({ runId }: { runId: string }) {
  const versions = useQuery({ queryKey: ["review-versions", runId], queryFn: () => researchApi.reviewVersions(runId) });
  const [sha, setSha] = useState("");
  const [displaySha, setDisplaySha] = useState("");
  const draft = useQuery({ queryKey: ["review-draft", runId, displaySha], queryFn: () => researchApi.readDraft(runId, displaySha), enabled: !!displaySha });
  const [urls, setUrls] = useState("");
  const [requirements, setRequirements] = useState<string[]>([]);
  const [amount, setAmount] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [plan, setPlan] = useState<{ signature: string; key: string; estimate: number | null; available: number | null }>();
  const [created, setCreated] = useState("");
  const data = versions.data;
  if (!data) return versions.isError ? <p role="alert">暂时无法读取复核版本。<button onClick={() => void versions.refetch()}>重试</button></p> : null;
  const base = sha || data.drafts.at(-1) || "";
  const sources = urls.split(/\s+/).map(value => value.trim()).filter(Boolean);
  const options = { base_sha256: base, max_cost_micro_usd: Math.round(Number(amount) * 1_000_000), urls: sources, requirement_ids: sources.length ? requirements : [] };
  const signature = JSON.stringify(options);
  const allowed = !!base && Number.isSafeInteger(options.max_cost_micro_usd) && options.max_cost_micro_usd > 0 && (!sources.length || requirements.length > 0);
  async function submit() {
    if (busy || !allowed) return;
    setBusy(true);
    setError("");
    try {
      if (!plan || plan.signature !== signature) {
        const preview = await researchApi.previewDraftReview(runId, options);
        setPlan({ signature, key: crypto.randomUUID(), estimate: preview.estimated_review_micro_usd, available: preview.monthly_available });
        setCreated("");
      } else {
        const child = await researchApi.createDraftReview(runId, options, plan.key);
        setCreated(child.run_id);
        await versions.refetch();
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "复核请求失败");
    } finally {
      setBusy(false);
    }
  }
  const history = data.execution;
  return <section className="panel review-versions" aria-label="复核版本与执行回执">
    {data.parent && <p>基础草稿来自 <Link href={`/research/${data.parent.parent_run_id}?view=report`}>父运行</Link>，SHA-256 为 <code>{data.parent.base_sha256}</code>。</p>}
    {data.children.length > 0 && <p>复核版本 {data.children.map(child => <Link key={child.run_id} href={`/research/${child.run_id}`}>{child.run_id.slice(0, 8)}（{child.review_passed ? "复核通过" : child.completion_status === "partial" ? "部分交付" : childStatuses[child.status] ?? child.status}） </Link>)}</p>}
    <details><summary>查看本次执行回执</summary>
      <p>研究任务 {Object.keys(history.tasks).length || "不可核实"}，实际抓取 {history.physical_fetches ?? "不可核实"}，缓存复用 {history.cache_reuses ?? "不可核实"}。</p>
      {history.unknown.length > 0 && <p>部分回执缺失，相关执行要求尚待核实。</p>}
      {history.skills.length > 0 && <ol>{history.skills.map((skill, i) => <li key={skill.sequence + ":" + i}>{skill.key} · 事件 {skill.sequence}{history.first_web_sequence != null && skill.sequence < history.first_web_sequence ? " · 首次网页读取前" : ""}</li>)}</ol>}
      {history.documents.length > 0 && <ul>{history.documents.map((doc, i) => <li key={i}>{doc.url} · {doc.cached ? "缓存复用" : "抓取"} · 读取时间 {doc.read_at ?? "不可核实"} · 发布日期 {doc.published_at ?? "不可核实"}</li>)}</ul>}
    </details>
    {data.delivered_sha256 && <p>本版本正文 SHA-256 <code>{data.delivered_sha256}</code></p>}
    {base && <details><summary>{data.can_create ? "基于归档草稿重新复核" : "查看归档草稿"}</summary>
      <div className="review-version-fields"><label>基础草稿<select value={base} onChange={e => setSha(e.target.value)}>{data.drafts.map(value => <option key={value} value={value}>第 {data.draft_attempts?.[value] ?? "?"} 稿 · {value.slice(0, 12)}</option>)}</select></label>
      <button type="button" className="secondary" onClick={() => setDisplaySha(displaySha === base ? "" : base)}>{displaySha === base ? "收起原文" : "查看归档原文"}</button>
      {displaySha === base && (draft.isError ? <p role="alert">归档原文读取失败。</p> : <pre>{draft.data?.markdown}</pre>)}
      {data.can_create && <><label>补充来源网址（可选，每行一条）<textarea value={urls} onChange={e => setUrls(e.target.value)} /></label>
      {sources.length > 0 && <fieldset><legend>这些来源需要核实哪些原始事实</legend>{data.requirements.map(item => <label key={item.id}><input type="checkbox" value={item.id} checked={requirements.includes(item.id)} onChange={e => setRequirements(e.target.checked ? [...requirements, item.id] : requirements.filter(id => id !== item.id))} />{item.subject ? `${item.subject} · ${item.text}` : item.text}</label>)}</fieldset>}
      <label>本次复核费用上限（美元）<input type="number" min="0.000001" step="any" value={amount} onChange={e => setAmount(e.target.value)} /></label>
      {plan?.signature === signature && <p>按基础版本模型估算，一轮审阅约 {plan.estimate == null ? "待核实" : money(plan.estimate)}。发起调用前，网关按实际输入计算所需预留。补证和修订共用本次上限 {money(options.max_cost_micro_usd)}。{plan.available != null && `本月可用 ${money(plan.available)}。`}</p>}
      {error && <p role="alert">{error}</p>}
      {created && plan?.signature === signature ? <Link href={`/research/${created}`}>打开复核任务</Link> :
        <button type="button" className="secondary" disabled={busy || !allowed} onClick={() => void submit()}>{plan?.signature === signature ? "确认费用并创建复核" : "查看复核费用"}</button>}
      </>}
      </div>
    </details>}
  </section>;
}
