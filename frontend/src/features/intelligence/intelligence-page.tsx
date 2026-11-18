"use client";

import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeSanitize from "rehype-sanitize";
import { AppShell } from "@/components/app-shell";
import { EmptyState, PageHeading, StatusBadge, SurfaceDialog } from "@/components/ui/workspace";
import { iamApi, localAuthBypass } from "@/lib/auth";
import { apiFetch } from "@/lib/api/http";
import { command, endpoint, errorText, fetchItems, fetchPlans, money, post, qualityNames, statusNames,
  type Item, type Notification, type Options, type Plan, type PlanDefinition, type Snapshot } from "./api";
import "./intelligence.css";

function useIdentity() {
  const identity = useQuery({ queryKey: ["identity"], queryFn: iamApi.me, enabled: !localAuthBypass, retry: false });
  return { actor: identity.data?.id ?? (localAuthBypass ? "local-dev" : ""), identity: identity.data };
}

function useAction() {
  const cache = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const act = async (run: () => Promise<unknown>) => {
    setBusy(true); setError("");
    try { await run(); await cache.invalidateQueries({ queryKey: ["intelligence"] }); return true; }
    catch (error) { setError(errorText(error)); return false; }
    finally { setBusy(false); }
  };
  return { busy, error, act };
}

function Frame({ title, description, children, actions }: { title: string; description: string; children: ReactNode; actions?: ReactNode }) {
  return <AppShell><div className="page intelligence-page"><PageHeading title={title} description={description} actions={actions} />{children}</div></AppShell>;
}

function Problem({ error }: { error: unknown }) { return error ? <p role="alert" className="intelligence-error">{typeof error === "string" ? error : errorText(error)}</p> : null; }
function DateText({ value }: { value: string | null }) { return <>{value ? new Date(value).toLocaleString("zh-CN") : "—"}</>; }

function WorkList({ items }: { items: Item[] }) {
  return <div className="intelligence-list">{items.map(item => <Link key={item.id} className="intelligence-card" href={`/intelligence/items/${item.id}`}>
    <div><StatusBadge status={item.status} label={statusNames[item.status]} /><span className="intelligence-muted">{qualityNames[item.quality_status]}</span></div>
    <h3>{item.question}</h3><p>{item.reason}</p><small><DateText value={item.create_time} /></small>
  </Link>)}{!items.length && <EmptyState title="暂无研究事项" description="来源发生变化后，研判结果与研究事项会显示在这里。" />}</div>;
}

function PlanForm({ options, initial, onSave, busy, disabled = false }: { options: Options; initial?: PlanDefinition; onSave: (plan: PlanDefinition) => void; busy: boolean; disabled?: boolean }) {
  const [base, setBase] = useState(initial?.knowledge_base_id ?? "");
  const [reviewers, setReviewers] = useState<string[]>(initial?.reviewer_ids ?? []);
  const [selected, setSelected] = useState<string[]>(initial?.sources.filter(s => s.kind === "knowledge").map(s => s.document_id!) ?? []);
  const chosen = options.bases.find(value => value.id === base);
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); if (!chosen) return;
    const data = new FormData(event.currentTarget);
    const urls = String(data.get("urls") ?? "").split(/\n/).map(url => url.trim()).filter(Boolean);
    onSave({ workspace_id: chosen.workspace_id, knowledge_base_id: base, name: String(data.get("name")).trim(), objective: String(data.get("objective")).trim(),
      dimensions: String(data.get("dimensions") ?? "").split(/[，,\n]/).map(value => value.trim()).filter(Boolean),
      sources: [...urls.map(url => ({ kind: "web" as const, url, document_id: null, label: "" })), ...selected.map(id => ({ kind: "knowledge" as const, url: null, document_id: id, label: "" }))],
      budget: { run_micro_usd: Math.round(Number(data.get("runBudget")) * 1_000_000), month_micro_usd: Math.round(Number(data.get("monthBudget")) * 1_000_000), assessment_micro_usd: initial?.budget.assessment_micro_usd ?? 100_000 },
      cadence: String(data.get("cadence")) as PlanDefinition["cadence"], timezone: "Asia/Shanghai", mode: initial?.mode ?? "observe",
      configurable: initial?.configurable ?? {}, profile_revision_id: initial?.profile_revision_id ?? null, reviewer_ids: reviewers });
  };
  return <form className="intelligence-form" onSubmit={submit}>
    <label>计划名称<input name="name" required maxLength={160} defaultValue={initial?.name} placeholder="例如：重点竞品定价与功能变化" /></label>
    <label>工作空间与知识库<select aria-label="工作空间与知识库" value={base} required disabled={Boolean(initial)} onChange={e => { setBase(e.target.value); setSelected([]); setReviewers([]); }}>
      <option value="">选择成果所属的知识库</option>{options.bases.filter(b => b.can_submit).map(b => <option key={b.id} value={b.id}>{b.workspace_name} / {b.name}</option>)}</select></label>
    <label>持续关注的问题<textarea name="objective" required maxLength={12000} rows={4} defaultValue={initial?.objective} placeholder="明确关注对象、比较条件，以及哪些变化会影响决策。" /></label>
    <label>比较维度<input name="dimensions" defaultValue={initial?.dimensions.join("，")} placeholder="价格，套餐限制，新增能力" /></label>
    <label>公开网页来源<textarea name="urls" rows={4} defaultValue={initial?.sources.filter(s => s.kind === "web").map(s => s.url).join("\n")} placeholder="每行一个完整 URL" /></label>
    {chosen && <fieldset><legend>已发布知识资料</legend>{options.documents.filter(d => d.knowledge_base_id === base).map(doc => <label className="intelligence-check" key={doc.id}><input type="checkbox" checked={selected.includes(doc.id)} onChange={e => setSelected(e.target.checked ? [...selected, doc.id] : selected.filter(id => id !== doc.id))} />{doc.filename}</label>)}</fieldset>}
    {chosen && <fieldset><legend>通知审阅者（对方需自行绑定并开启飞书通知）</legend>{options.members?.filter(m => m.knowledge_base_id === base && m.can_review).map(member => <label className="intelligence-check" key={member.user_id}><input type="checkbox" checked={reviewers.includes(member.user_id)} onChange={e => setReviewers(e.target.checked ? [...reviewers, member.user_id] : reviewers.filter(id => id !== member.user_id))} />{member.name}</label>)}</fieldset>}
    <div className="intelligence-grid"><label>检查频率<select name="cadence" defaultValue={initial?.cadence ?? "daily"}><option value="manual">手动</option><option value="hourly">每小时</option><option value="daily">每天</option><option value="weekly">每周</option></select></label>
      <label>单次研究上限（美元）<input type="number" name="runBudget" min="0.01" step="0.01" required defaultValue={initial ? initial.budget.run_micro_usd / 1e6 : undefined} /></label>
      <label>计划月度上限（美元）<input type="number" name="monthBudget" min="0.1" step="0.01" required defaultValue={initial ? initial.budget.month_micro_usd / 1e6 : undefined} /></label></div>
    <p className="intelligence-muted">新计划先处于草稿；启用时确认授权。每次影响研判上限为 {money(initial?.budget.assessment_micro_usd ?? 100000)}，计入计划月度额度。正式知识由人审阅发布。</p>
    {disabled && <p role="alert">计划已更新，请关闭此窗口并重新载入后编辑。</p>}
    <button type="submit" disabled={busy || disabled || !chosen}>{busy ? "正在保存…" : initial ? "保存并暂停，等待重新授权" : "创建计划草稿"}</button>
  </form>;
}

function FeishuPanel({ actor }: { actor: string }) {
  const binding = useQuery({ queryKey: ["intelligence", actor, "feishu"], queryFn: () => apiFetch<{ bound: boolean; enabled: boolean }>("/integrations/feishu"), enabled: Boolean(actor), retry: false });
  const { act, busy, error } = useAction();
  return <section className="intelligence-card"><h2>飞书通知</h2><p>仅发送状态提醒与回站链接。授权、反馈和知识发布在 MarketScout 完成。</p><Problem error={error || binding.error} />
    {binding.data?.bound ? <div className="intelligence-actions"><button disabled={busy} onClick={() => void act(() => apiFetch("/integrations/feishu", { ...post({ enabled: !binding.data?.enabled }), method: "PATCH" }))}>{binding.data.enabled ? "关闭通知" : "开启通知"}</button>
      <button disabled={busy} onClick={() => void act(() => apiFetch("/integrations/feishu", { method: "DELETE" }))}>解除绑定</button></div> : <button disabled={busy || localAuthBypass} onClick={() => void act(async () => { const value = await apiFetch<{ url: string }>("/integrations/feishu/bind", post()); window.location.assign(value.url); })}>绑定本人飞书账号</button>}
  </section>;
}

export function IntelligencePage() {
  const { actor } = useIdentity(); const router = useRouter();
  const [workspace, setWorkspace] = useState(""); const [creating, setCreating] = useState(false);
  const options = useQuery({ queryKey: ["intelligence", actor, "options"], queryFn: () => apiFetch<Options>(`${endpoint}/options`), enabled: Boolean(actor), retry: false });
  const plans = useQuery({ queryKey: ["intelligence", actor, workspace, "plans"], queryFn: () => fetchPlans(workspace), enabled: Boolean(actor), refetchInterval: 5000, retry: false });
  const work = useQuery({ queryKey: ["intelligence", actor, workspace, "items"], queryFn: () => fetchItems(undefined, workspace), enabled: Boolean(actor), refetchInterval: 5000, retry: false });
  const inbox = useQuery({ queryKey: ["intelligence", actor, workspace, "inbox"], queryFn: () => apiFetch<{ items: Notification[] }>(`${endpoint}/inbox${workspace ? `?workspace_id=${encodeURIComponent(workspace)}` : ""}`), enabled: Boolean(actor), refetchInterval: 5000, retry: false });
  const { act, busy, error } = useAction();
  const spaces = [...new Map((options.data?.bases ?? []).map(b => [b.workspace_id, b.workspace_name])).entries()];
  const pending = (work.data?.items ?? []).filter(item => ["proposed", "blocked", "awaiting_review"].includes(item.status));
  return <Frame title="持续情报" description="跟踪变化，核实影响，让重要判断保持有据可查。" actions={<button onClick={() => setCreating(true)} disabled={!options.data}>新建跟踪计划</button>}>
    <Problem error={error || plans.error || options.error} />
    <label className="intelligence-filter">工作空间<select value={workspace} onChange={e => setWorkspace(e.target.value)}><option value="">所有可访问空间</option>{spaces.map(([id, name]) => <option value={id} key={id}>{name}</option>)}</select></label>
    <section><h2>需要你处理 <span className="intelligence-count">{pending.length}</span></h2><WorkList items={pending} /></section>
    <section><h2>跟踪计划</h2><div className="intelligence-grid">{plans.data?.items.map(plan => <Link className="intelligence-card" key={plan.id} href={`/intelligence/plans/${plan.id}`}><StatusBadge status={plan.status} label={statusNames[plan.status]} /><h3>{plan.name}</h3><p>{plan.objective}</p><small>{plan.mode === "observe" ? "观察与建议" : "自动研究 · 人工发布"}</small></Link>)}</div>
      {plans.data && !plans.data.items.length && <EmptyState title="建立第一项持续跟踪" description="选择问题、资料范围和费用上限，从观察模式开始。" />}</section>
    <section className="intelligence-card"><h2>通知</h2>{inbox.data?.items.filter(n => !n.read_at).slice(0, 12).map(n => <div key={n.id} className="intelligence-notice"><Link href={n.item_id ? `/intelligence/items/${n.item_id}` : `/intelligence/plans/${n.plan_id}`}>{n.kind === "review_ready" ? "研究成果待审阅" : n.kind === "research_proposed" ? "有新的研究建议" : "计划状态需要查看"}</Link><DateText value={n.create_time} /><button disabled={busy} onClick={() => void act(() => apiFetch(`${endpoint}/inbox/${n.id}/read`, post()))}>标为已读</button></div>)}</section>
    <FeishuPanel actor={actor} />
    <SurfaceDialog title="新建持续跟踪计划" open={creating} onOpenChange={setCreating}>{options.data && <PlanForm options={options.data} busy={busy} onSave={definition => void act(async () => { const plan = await apiFetch<Plan>(`${endpoint}/plans`, post(definition)); router.push(`/intelligence/plans/${plan.id}`); })} />}<Problem error={error} /></SurfaceDialog>
  </Frame>;
}

function BaselineForm({ plan, options, busy, onSave }: { plan: Plan; options: Options; busy: boolean; onSave: (facts: string[], pages: string[]) => void }) {
  const [facts, setFacts] = useState(plan.baseline.fact_ids ?? []);
  const [pages, setPages] = useState(plan.baseline.wiki_revision_ids ?? []);
  return <form className="intelligence-form" onSubmit={e => { e.preventDefault(); onSave(facts, pages); }}>
    <p>选择本计划需要持续复核的已发布结论。关联基线不会发布新知识，也不会扩大网页与资料的取证范围。</p>
    <fieldset><legend>已发布事实</legend>{options.facts?.filter(f => f.knowledge_base_id === plan.knowledge_base_id).map(f => <label className="intelligence-check" key={f.id}><input type="checkbox" checked={facts.includes(f.id)} onChange={e => setFacts(e.target.checked ? [...facts, f.id] : facts.filter(id => id !== f.id))} />{f.name}</label>)}</fieldset>
    <fieldset><legend>已发布 Wiki</legend>{options.wiki_revisions?.filter(p => p.knowledge_base_id === plan.knowledge_base_id).map(p => <label className="intelligence-check" key={p.id}><input type="checkbox" checked={pages.includes(p.id)} onChange={e => setPages(e.target.checked ? [...pages, p.id] : pages.filter(id => id !== p.id))} />{p.name}</label>)}</fieldset>
    <button disabled={busy}>保存已发布基线</button>
  </form>;
}

export function PlanPage({ id }: { id: string }) {
  const [authorizationDates] = useState(() => ({ today: new Date().toISOString().slice(0, 10), expiry: new Date(Date.now() + 30 * 864e5).toISOString().slice(0, 10) }));
  const { actor } = useIdentity(); const [editing, setEditing] = useState(false); const [authorizing, setAuthorizing] = useState(false); const [newOwner, setNewOwner] = useState("");
  const router = useRouter(); const [requesting, setRequesting] = useState(false); const [linking, setLinking] = useState(false);
  const [editingVersion, setEditingVersion] = useState(0); const [authorizationVersion, setAuthorizationVersion] = useState(0); const [requestVersion, setRequestVersion] = useState(0);
  const plan = useQuery({ queryKey: ["intelligence", actor, id, "plan"], queryFn: () => apiFetch<Plan>(`${endpoint}/plans/${id}`), enabled: Boolean(actor), refetchInterval: 5000, retry: false });
  const options = useQuery({ queryKey: ["intelligence", actor, "options"], queryFn: () => apiFetch<Options>(`${endpoint}/options`), enabled: Boolean(actor), retry: false });
  const grants = useQuery({ queryKey: ["intelligence", actor, id, "authorizations"], queryFn: () => apiFetch<{ items: { id: string; plan_version: number; expires_at: string; revoked_at: string | null }[] }>(`${endpoint}/plans/${id}/authorizations`), enabled: Boolean(actor), refetchInterval: 5000, retry: false });
  const items = useQuery({ queryKey: ["intelligence", actor, id, "items"], queryFn: () => fetchItems(id), enabled: Boolean(actor), refetchInterval: 5000 });
  const decisions = useQuery({ queryKey: ["intelligence", actor, id, "decisions"], queryFn: () => apiFetch<{ items: { id: string; would_start: boolean; assessment: { decision: string; reason: string }; create_time: string }[] }>(`${endpoint}/plans/${id}/decisions`), enabled: Boolean(actor), refetchInterval: 5000 });
  const { act, busy, error } = useAction(); const value = plan.data;
  const canManage = Boolean(value?.capabilities?.manage);
  const control = (action: string) => value && void act(() => apiFetch(`${endpoint}/plans/${id}/${action}`, post(command(value.version))));
  const authorize = (event: FormEvent<HTMLFormElement>) => { event.preventDefault(); if (!value) return; const data = new FormData(event.currentTarget); void act(async () => { await apiFetch(`${endpoint}/plans/${id}/activate`, post({ ...command(authorizationVersion), mode: data.get("mode"), expires_at: new Date(String(data.get("expiration")) + "T23:59:59+08:00").toISOString() })); setAuthorizing(false); }); };
  return <Frame title={value?.name ?? "跟踪计划"} description={value?.objective ?? "正在读取计划…"} actions={<Link href="/intelligence">返回持续情报</Link>}>
    <Problem error={error || plan.error} />{value && <>
      <div className="intelligence-actions"><StatusBadge status={value.status} label={statusNames[value.status]} /><button disabled={busy || !value.capabilities?.authorize || value.status === "archived"} onClick={() => { setAuthorizationVersion(value.version); setAuthorizing(true); }}>启用或更新授权</button><button disabled={busy || !canManage || value.status !== "active"} onClick={() => control("pause")}>暂停计划</button><button disabled={busy || !canManage || value.status !== "active"} onClick={() => void act(() => apiFetch(`${endpoint}/plans/${id}/check`, post()))}>立即检查</button><button disabled={busy || !canManage} onClick={() => { setEditingVersion(value.version); setEditing(true); }}>编辑计划</button><button disabled={busy || !canManage} onClick={() => control("authorizations/revoke")}>撤销授权并停止研究</button><button disabled={busy || !canManage} onClick={() => control("archive")}>归档</button></div>
      {canManage && <section className="intelligence-card"><h2>移交计划</h2><p>移交后暂停自动工作，由新负责人确认授权和费用额度。历史运行与费用归属保留。</p><div className="intelligence-actions"><select aria-label="新负责人" value={newOwner} onChange={e => setNewOwner(e.target.value)}><option value="">选择新负责人</option>{options.data?.members?.filter(m => m.knowledge_base_id === value.knowledge_base_id && m.user_id !== value.owner_id).map(m => <option key={m.user_id} value={m.user_id}>{m.name}</option>)}</select><button disabled={busy || !newOwner} onClick={() => void act(() => apiFetch(`${endpoint}/plans/${id}/transfer`, post({ ...command(value.version), new_owner_id: newOwner })))}>移交并暂停</button></div></section>}
      <div className="intelligence-grid"><section className="intelligence-card"><h2>费用边界</h2><p>单次研究 {money(value.definition.budget.run_micro_usd)} · 月度 {money(value.definition.budget.month_micro_usd)}</p>{value.budget_periods?.map(p => <p key={p.period}>{p.period}：已结算 {money(p.settled)}，预留 {money(p.reserved)}</p>)}<small>美元额度覆盖模型调用，另受负责人个人配额限制；搜索等非模型服务按调用配额控制，未确认结果继续占用预留。</small><p><button disabled={busy || !canManage} onClick={() => void act(() => apiFetch(`${endpoint}/plans/${id}/reconcile-budget`, post()))}>按运行回执核对费用</button></p></section>
        <section className="intelligence-card"><h2>已发布基线</h2>{value.baseline.work_item_id ? <Link href={`/intelligence/items/${value.baseline.work_item_id}`}>查看最近审阅发布的研究</Link> : <p>{(value.baseline.fact_ids?.length ?? 0) + (value.baseline.wiki_revision_ids?.length ?? 0) ? `已关联 ${value.baseline.fact_ids?.length ?? 0} 条事实、${value.baseline.wiki_revision_ids?.length ?? 0} 篇 Wiki。` : "尚未建立。观察与研究结果不会自动变更正式知识。"}</p>}<button disabled={!canManage || !options.data} onClick={() => setLinking(true)}>关联已有结论</button></section></div>
      <section className="intelligence-card"><h2>授权记录</h2><Problem error={grants.error} />{grants.data?.items.length ? grants.data.items.slice(0, 5).map(grant => <p key={grant.id}>计划版本 {grant.plan_version} · {grant.revoked_at ? "已撤销" : "到期时间"} · <DateText value={grant.revoked_at ?? grant.expires_at} /></p>) : <p>尚未签发长期授权。</p>}</section>
      <section className="intelligence-card"><h2>来源检查</h2><div className="intelligence-table"><table><thead><tr><th>来源</th><th>观察版本</th><th>最近检查</th><th>下次检查</th><th>状态</th></tr></thead><tbody>{value.sources?.map(s => <tr key={s.id}><td>{s.definition.label || s.definition.url || "知识库资料"}</td><td>{s.sequence}</td><td><DateText value={s.last_checked_at} /></td><td><DateText value={s.next_run_at} /></td><td>{s.error || "正常"}</td></tr>)}</tbody></table></div></section>
      <section><h2>研究事项</h2><button disabled={!canManage || !["active", "paused"].includes(value.status)} onClick={() => { setRequestVersion(value.version); setRequesting(true); }}>发起人工复核</button><WorkList items={items.data?.items ?? []} /></section>
      <section className="intelligence-card"><h2>变化判断记录</h2>{decisions.data?.items.map(d => <article className="intelligence-decision" key={d.id}><strong>{d.would_start ? "观察模式：建议研究" : d.assessment.decision === "ignore" ? "无需研究" : d.assessment.decision === "research" ? "需要研究" : "需要人工判断"}</strong><p>{d.assessment.reason}</p><small><DateText value={d.create_time} /></small></article>)}</section>
      <SurfaceDialog title="确认持续研究授权" open={authorizing} onOpenChange={setAuthorizing}><form className="intelligence-form" onSubmit={authorize}><p>将使用你的账户额度，在下面范围内采集资料与研究，并评估目标知识库内的发布事件。正式知识始终需要人工发布。更新授权会撤销旧版授权；旧版授权下的执行将停止，可在新版授权下开展补充研究。</p><ul>{value.definition.sources.map((s, i) => <li key={i}>{s.url || s.label || "所选知识库资料"}</li>)}</ul><p>单次研究上限 {money(value.definition.budget.run_micro_usd)}；计划月度上限 {money(value.definition.budget.month_micro_usd)}。</p><label>执行模式<select name="mode" defaultValue={value.mode}><option value="observe">观察：采集和研判，每次研究由人启动</option><option value="auto">自动研究：有价值的变化自动启动研究</option></select></label><label>授权到期日期（北京时间当日结束）<input name="expiration" type="date" required min={authorizationDates.today} defaultValue={authorizationDates.expiry} /></label><label className="intelligence-check"><input type="checkbox" required />我确认来源、动作范围和费用上限</label><button disabled={busy || authorizationVersion !== value.version}>确认授权并启用</button>{authorizationVersion !== value.version && <p role="alert">计划已更新，请关闭此窗口并重新检查授权范围。</p>}<Problem error={error} /></form></SurfaceDialog>
      <SurfaceDialog title="编辑跟踪计划" open={editing} onOpenChange={setEditing}>{options.data && <PlanForm options={options.data} initial={value.definition} busy={busy} disabled={editingVersion !== value.version} onSave={definition => void act(async () => { await apiFetch(`${endpoint}/plans/${id}`, { ...post({ ...command(editingVersion), plan: definition }), method: "PATCH" }); setEditing(false); })} />}<Problem error={error} /></SurfaceDialog>
      <SurfaceDialog title="关联已有结论" open={linking} onOpenChange={setLinking}>{linking && options.data && <BaselineForm key={value.version} plan={value} options={options.data} busy={busy} onSave={(facts, pages) => void act(async () => { await apiFetch(`${endpoint}/plans/${id}/baseline`, post({ ...command(value.version), fact_ids: facts, wiki_revision_ids: pages })); setLinking(false); })} />}<Problem error={error} /></SurfaceDialog>
      <SurfaceDialog title="发起人工复核" open={requesting} onOpenChange={setRequesting}><form className="intelligence-form" onSubmit={e => { e.preventDefault(); const question = String(new FormData(e.currentTarget).get("question")); void act(async () => { const item = await apiFetch<Item>(`${endpoint}/plans/${id}/requests`, post({ ...command(requestVersion), question })); router.push(`/intelligence/items/${item.id}`); }); }}><p>使用本计划当前授权与费用上限，核实你提出的问题。观察模式也可显式启动研究。</p><label>需要核实的问题<textarea name="question" required maxLength={8000} rows={4} /></label><button disabled={busy || requestVersion !== value.version}>创建复核研究</button>{requestVersion !== value.version && <p role="alert">计划已更新，请重新打开复核请求以确认当前范围。</p>}<Problem error={error} /></form></SurfaceDialog>
    </>}
  </Frame>;
}

function SnapshotReader({ snapshot, actor }: { snapshot: Snapshot; actor: string }) {
  const [offset, setOffset] = useState(0);
  const content = useQuery({ queryKey: ["intelligence", actor, snapshot.id, "snapshot", offset], queryFn: () => apiFetch<Snapshot>(`${endpoint}/snapshots/${snapshot.id}?offset=${offset}&limit=8000`) });
  return <article className="intelligence-card"><h3>{snapshot.metadata.title || snapshot.metadata.url}</h3><p><DateText value={snapshot.observed_at} /> · 来源日期：{snapshot.metadata.published_at || "未知"}</p><Problem error={content.error} /><pre className="intelligence-source">{content.data?.content}</pre><div className="intelligence-actions"><button disabled={!offset} onClick={() => setOffset(Math.max(0, offset - 8000))}>上一页</button><button disabled={content.data?.next_offset == null} onClick={() => setOffset(content.data!.next_offset!)}>下一页</button></div></article>;
}

type HistoryEntry = { run_id: string; create_time: string; result: { summary?: string; report?: string; runtime_status?: string; quality_status?: string } };

function EvidenceChange({ event, snapshots, actor }: { event: NonNullable<Item["manifest"]["events"]>[number]; snapshots: Snapshot[]; actor: string }) {
  const [expanded, setExpanded] = useState(false);
  const before = snapshots.find(s => s.id === event.previous_snapshot_id), after = snapshots.find(s => s.id === event.snapshot_id);
  return <details className="intelligence-card" onToggle={e => setExpanded(e.currentTarget.open)}><summary>新旧观察对比 · <DateText value={event.create_time} /></summary>{expanded && <div className="intelligence-grid">
    <section><h3>变化前</h3>{before ? <SnapshotReader snapshot={before} actor={actor} /> : <p>此前没有可用观察基线。</p>}</section>
    <section><h3>变化后</h3>{after ? <SnapshotReader snapshot={after} actor={actor} /> : <p>知识发布事件，请结合已发布结论核对。</p>}</section>
  </div>}</details>;
}

function PreviousRun({ run, itemId, actor }: { run: HistoryEntry; itemId: string; actor: string }) {
  const [expanded, setExpanded] = useState(false);
  const detail = useQuery({ queryKey: ["intelligence", actor, itemId, "history", run.run_id],
    queryFn: () => apiFetch<HistoryEntry>(`${endpoint}/items/${itemId}/runs/${encodeURIComponent(run.run_id)}`), enabled: expanded, retry: false });
  return <details onToggle={event => setExpanded(event.currentTarget.open)}><summary><DateText value={run.create_time} /> · {run.result.quality_status ? qualityNames[run.result.quality_status] : "等待运行回执"}</summary>
    <Problem error={detail.error} />{expanded && !detail.error && <ReactMarkdown disallowedElements={["img"]} remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSanitize]}>{detail.data?.result.report ?? run.result.summary ?? "正在读取研究成果…"}</ReactMarkdown>}</details>;
}

export function ItemPage({ id }: { id: string }) {
  const { actor } = useIdentity(); const [reason, setReason] = useState(""); const [snapshot, setSnapshot] = useState<string>();
  const item = useQuery({ queryKey: ["intelligence", actor, id, "item"], queryFn: () => apiFetch<Item>(`${endpoint}/items/${id}`), enabled: Boolean(actor), refetchInterval: 5000, retry: false });
  const history = useQuery({ queryKey: ["intelligence", actor, id, "history"], queryFn: () => apiFetch<{ items: HistoryEntry[] }>(`${endpoint}/items/${id}/runs`), enabled: Boolean(actor), refetchInterval: 5000 });
  const value = item.data;
  const canManage = Boolean(value?.capabilities?.manage);
  const canReview = Boolean(value?.capabilities?.review);
  const plan = useQuery({ queryKey: ["intelligence", actor, value?.plan_id, "plan"], queryFn: () => apiFetch<Plan>(`${endpoint}/plans/${value!.plan_id}`), enabled: Boolean(actor && value) });
  const { act, busy, error } = useAction();
  const action = (action: string) => value && void act(() => apiFetch(`${endpoint}/items/${id}/actions`, post({ ...command(value.version), action, reason })));
  const review = (action: string) => value && void act(async () => {
    let published: string[] = [];
    if (action === "complete" && value.review.wiki_page_id && plan.data) {
      const page = await apiFetch<{ published_revision_id: string | null }>(`/knowledge-bases/${plan.data.knowledge_base_id}/pages/${value.review.wiki_page_id}`);
      if (!page.published_revision_id) throw new Error("请先在知识台账审阅并发布 Wiki 草稿。");
      published = [page.published_revision_id];
    }
    await apiFetch(`${endpoint}/items/${id}/review`, post({ ...command(value.version), action, reason, published_wiki_revision_ids: published }));
  });
  return <Frame title="研究事项" description={value?.question ?? "正在读取研究事项…"} actions={<Link href={value ? `/intelligence/plans/${value.plan_id}` : "/intelligence"}>返回计划</Link>}>
    <Problem error={error || item.error} />{value && <>
      <div className="intelligence-actions"><StatusBadge status={value.status} label={statusNames[value.status]} /><strong>{qualityNames[value.quality_status]}</strong>{value.owner_id === actor && value.run_id && <Link href={`/research/${value.run_id}`}>查看研究执行详情</Link>}</div>
      {value.reason && <p>{value.reason}</p>}
      {value.result.pending_decision && <section className="intelligence-card"><h2>等待负责人决定</h2><p>{value.result.pending_decision.question}</p><p>来源范围或费用发生变化时，请更新计划授权，并按新的范围开展补充研究。</p></section>}
      <section className="intelligence-card"><h2>处理事项</h2><label>反馈或处理理由<textarea rows={3} value={reason} onChange={e => setReason(e.target.value)} placeholder="补充问题、指出具体证据，或说明忽略原因" /></label><div className="intelligence-actions">
        {value.status === "proposed" && <button disabled={busy || !canManage} onClick={() => action("start")}>启动这次研究</button>}
        {value.status === "running" && <button disabled={busy || !canManage || !reason.trim()} onClick={() => action("feedback")}>发送研究反馈</button>}
        {value.status === "blocked" && <button disabled={busy || !canManage} onClick={() => action("retry")}>重试原研究</button>}
        {["awaiting_review", "completed", "blocked"].includes(value.status) && <button disabled={busy || !canManage || !reason.trim()} onClick={() => action("supplement")}>按补充说明开展新一轮研究</button>}
        {["running", "queued", "blocked"].includes(value.status) && <button disabled={busy || !canManage} onClick={() => action("cancel")}>取消研究</button>}
        {["proposed", "blocked", "awaiting_review"].includes(value.status) && <button disabled={busy || !canManage || !reason.trim()} onClick={() => action("dismiss")}>忽略并记录理由</button>}
      </div><small>反馈先持久化，执行侧在下一安全检查点应用。</small></section>
      {value.result.report && <section className="intelligence-card intelligence-report"><h2>变化简报</h2><ReactMarkdown disallowedElements={["img"]} remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSanitize]}>{value.result.report}</ReactMarkdown></section>}
      <section className="intelligence-card"><h2>质量检查</h2><p>审阅未通过或未完成时，成果保留为研究草稿。</p><p>报告审阅：{!value.result.report_review || value.result.report_review.skipped ? "尚未完成有效审阅" : value.result.report_review.degraded ? "降级结果，需要人工复核" : value.result.report_review.decision === "pass" ? "通过" : value.result.report_review.decision === "revise" ? "需要修订" : "未通过"}</p>{value.result.report_review?.issue_count !== undefined && <p>检查记录 {value.result.report_review.attempt ?? 0} 次，问题 {value.result.report_review.issue_count} 项。</p>}</section>
      <section className="intelligence-card"><h2>证据与冻结来源</h2>{value.result.evidence?.map(e => <details key={e.evidence_id}><summary>{e.claim}</summary><blockquote>{e.supporting_excerpt}</blockquote><p>{e.source_url}</p>{e.observation_snapshot_id && <button onClick={() => setSnapshot(e.observation_snapshot_id)}>核对原始快照</button>}</details>)}<div className="intelligence-actions">{value.manifest.snapshots?.map(s => <button key={s.id} onClick={() => setSnapshot(s.id)}>{s.metadata.title || s.metadata.url}</button>)}</div></section>
      {snapshot && value.manifest.snapshots?.find(s => s.id === snapshot) && <SnapshotReader actor={actor} snapshot={value.manifest.snapshots.find(s => s.id === snapshot)!} />}
      {Boolean(value.manifest.events?.length) && <section><h2>触发本次研究的变化</h2>{value.manifest.events?.map(event => <EvidenceChange key={event.id} event={event} snapshots={value.manifest.snapshots ?? []} actor={actor} />)}</section>}
      {Boolean(history.data?.items.length) && <section className="intelligence-card"><h2>关联研究历史</h2>{history.data?.items.map(run => <PreviousRun key={run.run_id} run={run} itemId={id} actor={actor} />)}</section>}
      <section className="intelligence-card"><h2>人工知识发布</h2><p>先审阅并发布引用资料，再将通过检查的提案保存为知识草稿。未通过质量验收时只能保留和继续研究。</p>
        <div className="intelligence-actions"><button disabled={busy || !canReview || value.quality_status !== "passed" || value.status !== "awaiting_review"} onClick={() => review("prepare_sources")}>1. 准备资料审阅</button>
          <button disabled={busy || !canReview || value.quality_status !== "passed" || !value.review.sources || value.status !== "awaiting_review"} onClick={() => review("prepare_knowledge")}>2. 生成知识草稿</button>
          <button disabled={busy || !canReview || !value.review.wiki_page_id || value.status !== "awaiting_review"} onClick={() => review("complete")}>3. 确认已发布并更新基线</button></div>
        {Object.entries(value.review.sources ?? {}).map(([sid, source]) => <p key={sid}><Link href={`/knowledge?review=1&document_id=${source.document_id}${source.generation_id ? `&generation_id=${source.generation_id}` : ""}`}>审阅引用资料</Link></p>)}
        {value.review.wiki_page_id && plan.data && <p><Link href={`/knowledge/ledger?kb_id=${plan.data.knowledge_base_id}&page_id=${value.review.wiki_page_id}`}>在知识台账审阅与发布 Wiki 草稿</Link></p>}
      </section>
    </>}
  </Frame>;
}

export function FeishuCallbackPage() {
  const params = useSearchParams(); const started = useRef(false); const [message, setMessage] = useState(() => params.get("code") && params.get("state") ? "正在验证飞书授权…" : "授权未完成，请返回持续情报重新绑定。");
  useEffect(() => { if (started.current) return; started.current = true; const code = params.get("code"), state = params.get("state");
    if (!code || !state) return;
    void apiFetch("/integrations/feishu/callback", post({ code, state })).then(() => { setMessage("飞书账号已绑定。请返回持续情报页面开启通知。"); window.history.replaceState(null, "", "/intelligence/feishu/callback"); }).catch(error => setMessage(errorText(error)));
  }, [params]);
  return <Frame title="飞书账号绑定" description={message}><Link href="/intelligence">返回持续情报</Link></Frame>;
}
