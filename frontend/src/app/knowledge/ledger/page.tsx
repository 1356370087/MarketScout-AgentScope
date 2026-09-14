"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { AppShell } from "@/components/app-shell";
import { apiFetch } from "@/lib/api";

type Fact = { id: string; entity_name: string; metric: string; value_text: string; value_numeric: string | null; unit: string; scale: string; currency: string; data_period: string; status: string; verification: string; adopted: boolean; evidence_count: number };
type Citation = { type: string; fact_assertion_id?: string; document_id?: string; generation_id?: string };
type Block = { id: string; content: string; block_type?: string; citations: Citation[]; generated_by?: string };
type Page = { id: string; title: string; can_edit: boolean; can_publish: boolean; current_revision: { number: number; status: string; blocks: Block[] }; history?: { revision_number: number; status: string }[] };
type Job = { id: string; kind: string; status: string; error?: string; result?: { knowledge_base_id?: string } };
type Detail = { evidence: { document_id: string; generation_id: string; excerpt: string }[]; alternatives: { id: string; value_text: string }[] };
const post = (body?: unknown): RequestInit => ({ method: "POST", headers: { "Content-Type": "application/json" }, ...(body !== undefined ? { body: JSON.stringify(body) } : {}) });

export default function LedgerPage() {
  const [bases, setBases] = useState<{ id: string; name: string }[]>([]);
  const [kb, setKb] = useState("");
  const [facts, setFacts] = useState<Fact[]>([]);
  const [pages, setPages] = useState<{ id: string; title: string; status: string }[]>([]);
  const [page, setPage] = useState<Page | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [entity, setEntity] = useState("");
  const [filter, setFilter] = useState("");
  const [title, setTitle] = useState("");
  const [template, setTemplate] = useState("company_profile");
  const [precheck, setPrecheck] = useState<{ id: string; document_count: number; fact_count: number; page_count: number } | null>(null);
  const [manual, setManual] = useState({ entity_name: "", metric: "", value_text: "", value_numeric: "", unit: "", currency: "", scale: "", data_period: "", condition_text: "", document_id: "", generation_id: "" });
  const root = `/knowledge-bases/${kb}`;
  const refresh = useCallback(async () => {
    if (!kb) return;
    const [f, p, j] = await Promise.all([
      apiFetch<{ items: Fact[] }>(`${root}/facts?entity=${encodeURIComponent(entity)}&status=${filter}`),
      apiFetch<{ items: { id: string; title: string; status: string }[] }>(`${root}/pages`),
      apiFetch<{ items: Job[] }>(`${root}/jobs`),
    ]);
    setFacts(f.items); setPages(p.items); setJobs(j.items);
  }, [kb, root, entity, filter]);
  useEffect(() => { void apiFetch<{ items: { id: string; name: string }[] }>("/knowledge-bases").then(r => { const requested = new URLSearchParams(window.location.search).get("kb_id"); setBases(r.items); setKb(r.items.find(b => b.id === requested)?.id ?? r.items[0]?.id ?? ""); }).catch(e => setError(String(e))); }, []);
  useEffect(() => { setPage(null); setDetail(null); setPrecheck(null); }, [kb]);
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (!kb || params.get("kb_id") !== kb) return;
    let active = true;
    const pageId = params.get("page_id"), factId = params.get("fact_id");
    if (pageId) void apiFetch<Page>(`${root}/pages/${encodeURIComponent(pageId)}?include_history=true`).then(p => { if (active) setPage(p); }).catch(e => { if (active) setError(String(e)); });
    if (factId) void apiFetch<Detail>(`${root}/facts/${encodeURIComponent(factId)}`).then(d => { if (active) setDetail(d); }).catch(e => { if (active) setError(String(e)); });
    return () => { active = false; };
  }, [kb, root]);
  useEffect(() => { void refresh().catch(e => setError(String(e))); }, [refresh]);
  useEffect(() => {
    if (!jobs.some(j => ["running", "queued"].includes(j.status))) return;
    const timer = setInterval(() => { void refresh().catch(e => setError(String(e))); }, 3000);
    return () => clearInterval(timer);
  }, [jobs, refresh]);
  async function act(action: () => Promise<void>) {
    setBusy(true); setError(""); setMessage("");
    try { await action(); await refresh(); } catch (e) { setError(String(e)); } finally { setBusy(false); }
  }
  async function loadPage(id: string) { setPage(await apiFetch<Page>(`${root}/pages/${id}?include_history=true`)); }
  return <AppShell><div className="page knowledge-page">
    <header className="page-header"><div><span className="eyebrow">KNOWLEDGE / FACTS &amp; WIKI</span><h1>事实台账与知识 Wiki</h1><p>候选经审核后发布；不同来源的冲突值保留证据，人工决定采信。</p><Link href="/knowledge">返回知识检索</Link></div></header>
    <label>知识库 <select aria-label="知识库" value={kb} onChange={e => setKb(e.target.value)}>{bases.map(b => <option key={b.id} value={b.id}>{b.name}</option>)}</select></label>
    {error && <p role="alert">{error}</p>}{message && <p role="status">{message}</p>}
    {!kb ? <p>请先在资料页面创建知识库并上传资料。</p> : <fieldset disabled={busy} style={{ border: 0, padding: 0 }}>
      <h2>事实台账</h2><div className="document-toolbar"><input aria-label="筛选企业" placeholder="筛选企业" value={entity} onChange={e => setEntity(e.target.value)} /><select aria-label="事实状态" value={filter} onChange={e => setFilter(e.target.value)}><option value="">全部状态</option>{["draft", "published", "rejected", "withdrawn"].map(s => <option key={s}>{s}</option>)}</select><button onClick={() => void act(refresh)}>刷新</button></div>
      <div style={{ overflowX: "auto" }}><table><thead><tr>{["企业 / 指标", "数值", "期间 / 条件", "状态", "证据", "操作"].map(t => <th key={t}>{t}</th>)}</tr></thead><tbody>{facts.map(f => <tr key={f.id}><td>{f.entity_name} / {f.metric}</td><td>{f.value_text} {f.currency}</td><td>{f.data_period}</td><td>{f.status} · {f.verification}{f.adopted && " · 已采信"}</td><td><button onClick={() => void act(async () => setDetail(await apiFetch<Detail>(`${root}/facts/${f.id}`)))}>{f.evidence_count} 条 · 对比</button></td><td>{f.status === "draft" && <><button onClick={() => void act(async () => { await apiFetch(`${root}/facts/${f.id}/publish`, post()); })}>发布</button><button onClick={() => void act(async () => { await apiFetch(`${root}/facts/${f.id}/reject`, post({ reason: "人工审核不通过" })); })}>拒绝</button></>}{f.status === "published" && <><button onClick={() => void act(async () => { await apiFetch(`${root}/facts/${f.id}/review`, post({ verification: "verified", adopted: true })); })}>核验并采信</button><button onClick={() => void act(async () => { await apiFetch(`${root}/facts/${f.id}/review`, post({ verification: "disputed", adopted: false })); })}>标记争议</button><button onClick={() => void act(async () => { await apiFetch(`${root}/facts/${f.id}/withdraw`, post()); })}>撤回</button></>}</td></tr>)}</tbody></table></div>
      {!facts.length && <p>暂无事实。发布资料后会自动提取可识别的候选，也可手工提交。</p>}
      {detail && <aside><h3>来源与同口径断言</h3>{detail.evidence.map((e, i) => <p key={i}><Link href={`/documents/${e.document_id}?generation_id=${e.generation_id}`}>打开原件与代次</Link> {e.excerpt}</p>)}{detail.alternatives.map(a => <p key={a.id}>{a.value_text}</p>)}{!detail.alternatives.length && <p>没有其他已发布的同口径断言。</p>}</aside>}
      <details><summary>手工提交事实候选</summary><form onSubmit={e => { e.preventDefault(); void act(async () => { const { document_id, generation_id, ...value } = manual; await apiFetch(`${root}/facts`, post({ ...value, value_numeric: value.value_numeric || null, period_label: value.data_period, evidence: document_id && generation_id ? [{ document_id, generation_id }] : [] })); setMessage("事实已保存为待审核草稿。"); }); }}><div className="document-toolbar">{Object.entries({ entity_name: "企业 / 产品", metric: "指标", value_text: "原始数值或事实", value_numeric: "数值（可选）", unit: "单位", currency: "币种", scale: "数量级", data_period: "数据期间", condition_text: "适用条件", document_id: "来源文档 ID", generation_id: "来源代次 ID" }).map(([key, label]) => <label key={key}>{label}<input required={["entity_name", "metric", "value_text"].includes(key)} value={manual[key as keyof typeof manual]} onChange={e => setManual({ ...manual, [key]: e.target.value })} /></label>)}</div><button type="submit">保存事实草稿</button></form></details>
      <h2>Wiki</h2><div className="document-toolbar"><input aria-label="Wiki 标题" placeholder="Wiki 标题" value={title} onChange={e => setTitle(e.target.value)} /><select aria-label="Wiki 模板" value={template} onChange={e => setTemplate(e.target.value)}><option value="company_profile">竞品档案</option><option value="product_features">产品档案</option><option value="pricing_theme">定价主题</option></select><button disabled={!title.trim()} onClick={() => void act(async () => { const p = await apiFetch<{ id: string }>(`${root}/pages`, post({ title, template, entity_name: entity })); await loadPage(p.id); })}>创建 Wiki</button></div>
      <div className="document-toolbar">{pages.map(p => <button key={p.id} onClick={() => void act(() => loadPage(p.id))}>{p.title} · {p.status}</button>)}</div>
      {page && <section><h3>{page.title} · 修订 {page.current_revision.number} · {page.current_revision.status}</h3><p>事实段落必须绑定来源；个人判断请选择“分析”，生成内容保存为草稿。</p>
        {page.current_revision.blocks.map((b, index) => <div key={b.id} style={{ marginBottom: 16 }}><select aria-label={`段落 ${index + 1} 类型`} disabled={!page.can_edit} value={b.block_type ?? "text"} onChange={e => setPage({ ...page, current_revision: { ...page.current_revision, blocks: page.current_revision.blocks.map(x => x.id === b.id ? { ...x, block_type: e.target.value } : x) } })}><option value="text">事实</option><option value="heading">标题</option><option value="analysis">分析（人工判断）</option></select><textarea aria-label={`段落 ${index + 1}`} readOnly={!page.can_edit} rows={4} style={{ width: "100%" }} value={b.content} onChange={e => setPage({ ...page, current_revision: { ...page.current_revision, blocks: page.current_revision.blocks.map(x => x.id === b.id ? { ...x, content: e.target.value } : x) } })} /><p>来源：{b.citations.map((c, i) => <span key={i}>{c.type === "fact" ? facts.find(f => f.id === c.fact_assertion_id)?.value_text ?? c.fact_assertion_id : <Link href={`/documents/${c.document_id}?generation_id=${c.generation_id}`}>原件</Link>} </span>)}</p>{page.can_edit && <select aria-label={`段落 ${index + 1} 绑定事实`} value="" onChange={e => { if (e.target.value) setPage({ ...page, current_revision: { ...page.current_revision, blocks: page.current_revision.blocks.map(x => x.id === b.id ? { ...x, citations: [...x.citations, { type: "fact", fact_assertion_id: e.target.value }] } : x) } }); }}><option value="">绑定已发布事实…</option>{facts.filter(f => f.status === "published").map(f => <option key={f.id} value={f.id}>{f.entity_name} {f.metric} {f.value_text}</option>)}</select>}</div>)}
        <div className="document-toolbar">{page.can_edit && <><button onClick={() => void act(async () => { await apiFetch(`${root}/pages/${page.id}/draft`, { ...post({ base_revision: page.current_revision.number, blocks: page.current_revision.blocks }), method: "PUT" }); await loadPage(page.id); })}>保存 Wiki 草稿</button><button onClick={() => void act(async () => { await apiFetch(`${root}/pages/${page.id}/generate`, post({ base_revision: page.current_revision.number })); await loadPage(page.id); })}>依据事实生成草稿</button><button onClick={() => setPage({ ...page, current_revision: { ...page.current_revision, blocks: [...page.current_revision.blocks, { id: crypto.randomUUID(), content: "", citations: [], block_type: "text" }] } })}>添加段落</button></>}{page.can_publish && <button onClick={() => void act(async () => { await apiFetch(`${root}/pages/${page.id}/publish`, post()); await loadPage(page.id); })}>发布 Wiki</button>}<button onClick={() => void act(async () => { const r = await apiFetch<{ count: number }>(`${root}/pages/${page.id}/check-stale`, post()); setMessage(r.count ? `${r.count} 处来源已变化或需核验，请检查后保存新修订。` : "引用来源未发现变化。"); })}>检查来源更新</button></div>
        <details><summary>修订历史</summary>{page.history?.map(h => <p key={h.revision_number}>修订 {h.revision_number} · {h.status} {h.status === "published" && <button onClick={() => void act(async () => { const r = await apiFetch<{ blocks: string | Block[] }>(`${root}/pages/${page.id}/revisions/${h.revision_number}`); setMessage((typeof r.blocks === "string" ? JSON.parse(r.blocks) as Block[] : r.blocks).map(b => b.content).join("\n\n")); })}>阅读历史</button>}</p>)}</details>
      </section>}
      <h2>完整导入与导出</h2><p>归档包含原件与历史引用。导入在当前工作区创建独立知识库，需重新审核发布。</p><div className="document-toolbar"><button onClick={() => void act(async () => { await apiFetch(`${root}/exports`, post()); setMessage("导出已进入队列。"); })}>创建完整导出</button><label>上传归档预检 <input aria-label="上传归档预检" type="file" accept=".zip" onChange={e => { const file = e.target.files?.[0]; if (!file) return; void act(async () => { const form = new FormData(); form.append("file", file); const r = await apiFetch<{ ok: boolean; errors: string[]; id: string; document_count: number; fact_count: number; page_count: number }>(`${root}/imports/precheck`, { method: "POST", body: form }); if (!r.ok) throw new Error(r.errors.join("；")); setPrecheck(r); }); }} /></label></div>
      {precheck && <p>预检通过：{precheck.document_count} 份资料、{precheck.fact_count} 条事实、{precheck.page_count} 页 Wiki。<button onClick={() => void act(async () => { await apiFetch(`${root}/imports/${precheck.id}/confirm`, post()); setPrecheck(null); })}>确认导入为新知识库</button></p>}
      {jobs.map(j => <p key={j.id}>{j.kind} · {j.status} {j.error}{j.kind === "export" && j.status === "completed" && <a href={`/api/research${root}/exports/${j.id}/download`}>下载归档</a>}{j.result?.knowledge_base_id && <button onClick={() => void act(async () => { const b = await apiFetch<{ items: { id: string; name: string }[] }>("/knowledge-bases"); setBases(b.items); setKb(j.result!.knowledge_base_id!); })}>打开导入知识库</button>}</p>)}
    </fieldset>}
  </div></AppShell>;
}
