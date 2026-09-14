"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { AppShell } from "@/components/app-shell";
import { apiFetch } from "@/lib/api";
import styles from "./health.module.css";

const labels = { expired: "过期资料", missing_topic: "缺失主题", pending_review: "待核验内容", sync_failed: "同步失败", no_results: "检索无结果" };
type Kind = keyof typeof labels;
type Target = { id: string; company: string; period: string; topic: string; min_documents: number; max_age_days: number; available_documents: number; missing_documents: number };
type Health = {
  generated_at: string; counts: Record<Kind, number>; companies: string[]; periods: string[];
  groups: ({ company: string; period: string } & Record<Kind, number>)[];
  items: { kind: Kind; company: string; period: string; title: string; source_id: string; url: string; reason: string }[];
  targets: Target[]; total: number;
};
const initialTarget = { company: "", period: "", topic: "", min_documents: 1, max_age_days: 0 };

export default function HealthPage() {
  const [bases, setBases] = useState<{ id: string; name: string }[]>([]);
  const [kb, setKb] = useState("");
  const [company, setCompany] = useState("");
  const [period, setPeriod] = useState("");
  const [kind, setKind] = useState<Kind | "">("");
  const [days, setDays] = useState(30);
  const [offset, setOffset] = useState(0);
  const [reload, setReload] = useState(0);
  const [data, setData] = useState<Health | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [target, setTarget] = useState(initialTarget);
  const root = `/knowledge-bases/${kb}/health`;
  useEffect(() => {
    let active = true;
    void apiFetch<{ items: { id: string; name: string }[] }>("/knowledge-bases").then(r => {
      if (active) { setBases(r.items); setKb(r.items[0]?.id ?? ""); }
    }).catch(e => { if (active) setError(String(e)); });
    return () => { active = false; };
  }, []);
  useEffect(() => {
    if (!kb) return;
    let active = true;
    setLoading(true); setError(""); setData(null);
    const query = new URLSearchParams({ company, period, kind, days: String(days), offset: String(offset) });
    void apiFetch<Health>(`${root}?${query}`).then(r => { if (active) setData(r); })
      .catch(e => { if (active) setError(String(e).includes("403") ? "此看板需要知识库管理或审核权限。" : String(e)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [kb, root, company, period, kind, days, offset, reload]);
  function filter(action: () => void) { action(); setOffset(0); }
  async function saveTarget() {
    setSaving(true); setError("");
    try {
      await apiFetch(`${root}/targets`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(target) });
      setTarget(initialTarget); setReload(v => v + 1);
    } catch (e) { setError(String(e)); } finally { setSaving(false); }
  }
  async function removeTarget(id: string) {
    setSaving(true); setError("");
    try { await apiFetch(`${root}/targets/${id}`, { method: "DELETE" }); setReload(v => v + 1); }
    catch (e) { setError(String(e)); } finally { setSaving(false); }
  }
  return <AppShell><div className={`page knowledge-page ${styles.panel}`}>
    <header className="page-header"><div><span className="eyebrow">KNOWLEDGE / HEALTH</span><h1>知识库健康与缺口</h1><p>按竞品和数据期间检查资料覆盖、时效与待处理事项。</p><Link href="/knowledge">知识检索</Link> · <Link href="/knowledge/ledger">事实台账与 Wiki</Link></div></header>
    <div className={styles.toolbar}>
      <label>知识库 <select aria-label="知识库" value={kb} onChange={e => filter(() => { setKb(e.target.value); setCompany(""); setPeriod(""); setTarget(initialTarget); })}>{bases.map(b => <option key={b.id} value={b.id}>{b.name}</option>)}</select></label>
      <label>竞品 <input aria-label="竞品筛选" list="health-companies" placeholder="全部竞品" value={company} onChange={e => filter(() => setCompany(e.target.value))} /><datalist id="health-companies">{data?.companies.map(c => <option key={c}>{c}</option>)}</datalist></label>
      <label>数据期间 <input aria-label="期间筛选" list="health-periods" placeholder="全部期间" value={period} onChange={e => filter(() => setPeriod(e.target.value))} /><datalist id="health-periods">{data?.periods.map(p => <option key={p}>{p}</option>)}</datalist></label>
      <label>检索记录窗口 <select aria-label="检索窗口" value={days} onChange={e => filter(() => setDays(Number(e.target.value)))}>{[7, 30, 90, 365].map(n => <option key={n} value={n}>近 {n} 天</option>)}</select></label>
      <button disabled={loading || !kb} onClick={() => setReload(v => v + 1)}>刷新看板</button>
    </div>
    {error && <p role="alert">{error}</p>}{loading && <p role="status">正在汇总知识库健康状态…</p>}
    {!kb && !error && <p>暂无知识库，请先创建知识库。</p>}
    {data && <>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(140px,1fr))", gap: 12, margin: "20px 0" }}>
        {(Object.keys(labels) as Kind[]).map(k => <button key={k} aria-pressed={kind === k} onClick={() => filter(() => setKind(kind === k ? "" : k))} style={{ padding: 20, textAlign: "left", border: "1px solid var(--line)", borderRadius: 12 }}><span>{labels[k]}</span><strong style={{ display: "block", fontSize: 32 }}>{data.counts[k]}</strong></button>)}
      </div>
      <p>更新时间：{new Date(data.generated_at).toLocaleString()}。缺失主题按已配置目标计算，仅有效的已发布资料计入覆盖；检索记录仅展示本人，服务异常不计为业务缺口。</p>
      <h2>竞品与期间概览</h2>
      <div style={{ overflowX: "auto" }}><table style={{ width: "100%", textAlign: "left", borderSpacing: 12 }}><thead><tr><th>竞品</th><th>期间</th>{Object.values(labels).map(v => <th key={v}>{v}</th>)}</tr></thead><tbody>{data.groups.map(g => <tr key={`${g.company}/${g.period}`}><td><button onClick={() => filter(() => { setCompany(g.company); setPeriod(g.period); })}>{g.company}</button></td><td>{g.period}</td>{(Object.keys(labels) as Kind[]).map(k => <td key={k}>{g[k]}</td>)}</tr>)}</tbody></table></div>
      {!data.groups.length && <p>此范围暂无已标注资料或覆盖目标。</p>}
      <h2>待处理明细</h2><label>事项类型 <select aria-label="事项类型" value={kind} onChange={e => filter(() => setKind(e.target.value as Kind | ""))}><option value="">全部事项</option>{(Object.keys(labels) as Kind[]).map(k => <option key={k} value={k}>{labels[k]}</option>)}</select></label>
      <ul style={{ paddingLeft: 20 }}>{data.items.map((item, i) => <li key={`${item.kind}/${item.source_id}/${i}`} style={{ padding: "12px 0" }}><strong>{labels[item.kind]} · {item.company} · {item.period}</strong><p><Link href={item.url}>{item.title}</Link> — {item.reason}</p></li>)}</ul>
      {!data.items.length && <p>当前筛选下没有待处理事项；尚未配置覆盖目标时，不代表资料已完整。</p>}
      <div className="document-toolbar"><button disabled={!offset} onClick={() => setOffset(Math.max(0, offset - 50))}>上一页</button><span>共 {data.total} 条，当前 {data.total ? offset + 1 : 0}–{Math.min(offset + 50, data.total)}</span><button disabled={offset + 50 >= data.total} onClick={() => setOffset(offset + 50)}>下一页</button></div>
      <h2>主题覆盖目标</h2><p>按确认的竞品名称、期间及主题精确匹配。资料未设置“主题”时使用“资料类型”。更新周期填 0 表示仅检查明确有效期，历史资料不会仅因年份久远而过期。</p>
      <form onSubmit={e => { e.preventDefault(); void saveTarget(); }}><fieldset disabled={saving} style={{ border: 0, padding: 0 }}><div className={styles.toolbar}>
        <label>竞品名称 <input aria-label="目标竞品" required maxLength={200} value={target.company} onChange={e => setTarget({ ...target, company: e.target.value })} /></label>
        <label>数据期间 <input aria-label="目标期间" required maxLength={100} placeholder="例如 2025年" value={target.period} onChange={e => setTarget({ ...target, period: e.target.value })} /></label>
        <label>主题 / 资料类型 <input aria-label="目标主题" required maxLength={200} placeholder="例如 财报、价格表" value={target.topic} onChange={e => setTarget({ ...target, topic: e.target.value })} /></label>
        <label>最少资料数 <input aria-label="最少资料数" type="number" required min={1} max={100} value={target.min_documents} onChange={e => setTarget({ ...target, min_documents: Number(e.target.value) })} /></label>
        <label>更新周期（天，0 为不限）<input aria-label="更新周期" type="number" required min={0} max={3650} value={target.max_age_days} onChange={e => setTarget({ ...target, max_age_days: Number(e.target.value) })} /></label>
        <button type="submit">保存覆盖目标</button>
      </div></fieldset></form>
      {!data.targets.length && <p>尚未配置目标。添加竞品、期间和所需主题后，即使没有上传资料，也能看到对应缺口。</p>}
      <ul>{data.targets.filter(t => (!company || t.company === company) && (!period || t.period === period)).map(t => <li key={t.id}>{t.company} · {t.period} · {t.topic}：有效资料 {t.available_documents}/{t.min_documents}，缺 {t.missing_documents} 份 <button disabled={saving} onClick={() => setTarget({ company: t.company, period: t.period, topic: t.topic, min_documents: t.min_documents, max_age_days: t.max_age_days })}>编辑</button><button disabled={saving} onClick={() => void removeTarget(t.id)}>移除目标</button></li>)}</ul>
    </>}
  </div></AppShell>;
}
