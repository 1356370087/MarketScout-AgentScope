"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Save, Copy, Download } from "lucide-react";
import { platformApi, platformFetch, configStatus, type ConfigRevision, type ConfigDocument, type Catalog, type CatalogField, type Application } from "@/lib/platform";
import { useDraftGuard } from "./admin-shell";
import { Heading, Alert, JsonInput, QuotaFields, PlatformModal } from "./ui";
import { LoadingSkeleton } from "@/components/ui/insight";
import { iamApi } from "@/lib/auth";

export default function ConfigurationEditorPage({ id }: { id: string }) {
  const revision = useQuery({ queryKey: ["platform", "configuration", id], queryFn: () => platformApi.configuration(id) });
  const catalog = useQuery({ queryKey: ["platform", "catalog"], queryFn: platformApi.catalog });
  if (revision.error || catalog.error) return <Alert error={revision.error || catalog.error} />;
  if (!revision.data || !catalog.data) return <LoadingSkeleton label="正在读取配置和字段说明…" />;
  return <ConfigurationEditor key={`${id}:${revision.data.version}`} revision={revision.data} catalog={catalog.data} />;
}

function ValueControl({ field, value, disabled, onChange, onInvalid }: { field: CatalogField; value: unknown; disabled: boolean; onChange: (value: unknown) => void; onInvalid: (invalid: boolean) => void }) {
  const schema = field.schema;
  const variants = schema.anyOf as Array<Record<string, unknown>> | undefined;
  const type = String(schema.type ?? variants?.find((item) => item.type !== "null")?.type ?? typeof value);
  const options = (schema.enum ?? variants?.find((item) => item.enum)?.enum) as unknown[] | undefined;
  if (options) return <select aria-label={field.label} disabled={disabled} value={String(value ?? "")} onChange={(event) => onChange(event.target.value)}>{options.map((item) => <option key={String(item)} value={String(item)}>{String(item)}</option>)}</select>;
  if (type === "boolean") return <label><input type="checkbox" disabled={disabled} checked={Boolean(value)} onChange={(event) => onChange(event.target.checked)} /> 启用</label>;
  if (type === "array" || type === "object" || typeof value === "object" && value !== null) return <JsonInput label={field.label} value={value ?? (type === "array" ? [] : {})} onChange={onChange} onInvalid={onInvalid} disabled={disabled} />;
  return <input aria-label={field.label} disabled={disabled} type={type === "integer" || type === "number" ? "number" : "text"} step={type === "integer" ? 1 : "any"} value={value == null ? "" : String(value)} onChange={(event) => onChange(event.target.value === "" ? null : type === "integer" || type === "number" ? Number(event.target.value) : event.target.value)} />;
}

function ConfigurationEditor({ revision, catalog }: { revision: ConfigRevision; catalog: Catalog }) {
  const router = useRouter();
  const cache = useQueryClient();
  const guard = useDraftGuard();
  const [document, setDocument] = useState<ConfigDocument>(structuredClone(revision.document));
  const [group, setGroup] = useState("research");
  const [search, setSearch] = useState("");
  const [error, setError] = useState<unknown>();
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [invalid, setInvalid] = useState<Record<string, boolean>>({});
  const [reason, setReason] = useState("");
  const [diff, setDiff] = useState<{ base_revision: string | null; changes: Array<{ field: string; before: unknown; after: unknown }> } | null>(null);
  const [rollback, setRollback] = useState(false);
  const state = useQuery({ queryKey: ["platform", "status"], queryFn: platformApi.status });
  const draft = revision.status === "draft";
  const global = revision.scope === "global";
  const identity = useQuery({ queryKey: ["identity"], queryFn: iamApi.me });
  const canEdit = identity.data?.permissions.includes(global ? "platform.config.write" : "platform.config.personal") === true;
  const canPublish = identity.data?.permissions.includes(global ? "platform.config.publish" : "platform.config.personal") === true;
  const editable = draft && canEdit;
  const applications = useQuery({ queryKey: ["platform", "applications", revision.id], queryFn: () => platformFetch<{ items: Application[] }>(`/configurations/${revision.id}/applications`), enabled: !draft });
  const groups = [...new Map(Object.values(catalog.runtime).filter((field) => !field.bootstrap && !field.secret && !field.derived).map((field) => [field.group, field.group_label])).entries()];
  const updateDocument = (next: ConfigDocument) => { setDocument(next); guard.setDirty(true); };
  const save = useCallback(async () => {
    if (Object.values(invalid).some(Boolean)) throw new Error("请先修正 JSON 配置格式。");
    await platformFetch(`/configurations/${revision.id}`, "PUT", { expected_version: revision.version, document });
    guard.setDirty(false); setNotice("草稿已保存。");
    await cache.invalidateQueries({ queryKey: ["platform"] });
  }, [invalid, revision.id, revision.version, document, guard, cache]);
  useEffect(() => { guard.registerSave(editable ? save : null); return () => guard.registerSave(null); }, [guard, editable, save]);
  async function act(action: () => Promise<void>) { setBusy(true); setError(undefined); try { await action(); } catch (cause) { setError(cause); } finally { setBusy(false); } }
  async function preparePublish() { if (guard.dirty) { setError(new Error("请先保存草稿，再校验发布。")); return; } await act(async () => { await platformFetch(`/configurations/${revision.id}/validate`, "POST"); setDiff(await platformFetch(`/configurations/${revision.id}/diff`)); }); }
  async function publish() { await act(async () => { const next = await platformFetch<ConfigRevision>(`/configurations/${revision.id}/${rollback ? "rollback" : "publish"}`, "POST", { expected_version: revision.version, expected_platform_revision: diff?.base_revision, reason }); setDiff(null); guard.setDirty(false); await Promise.all([cache.invalidateQueries({ queryKey: ["platform"] }), cache.invalidateQueries({ queryKey: ["capabilities"] }), cache.invalidateQueries({ queryKey: ["model-catalog"] })]); if (rollback) router.push(`/admin/configurations/${next.id}`); }); }
  async function copy() { await act(async () => { const next = await platformFetch<ConfigRevision>(`/configurations/${revision.id}/copy`, "POST"); guard.setDirty(false); router.push(`/admin/configurations/${next.id}`); }); }
  async function exportConfig() { const payload = await platformFetch(`/configurations/${revision.id}/export`); const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" })); const anchor = window.document.createElement("a"); anchor.href = url; anchor.download = `insightforge-config-v${revision.number}.json`; anchor.click(); URL.revokeObjectURL(url); }
  const fields = Object.values(catalog.runtime).filter((field) => !field.bootstrap && !field.secret && !field.derived && (global || catalog.policy[field.name]?.editable) && (search ? `${field.name} ${field.label} ${field.description}`.toLowerCase().includes(search.toLowerCase()) : field.group === group));
  return <><Heading title={revision.name} description={`v${revision.number} · ${global ? "平台配置" : "个人调试配置"} · ${configStatus[revision.status] ?? revision.status}`}><button className="secondary" onClick={() => void act(exportConfig)}><Download size={14} /> 导出</button><button className="secondary" disabled={!canEdit} onClick={() => void copy()}><Copy size={14} /> 复制草稿</button>{draft && <><button className="secondary" disabled={busy || !canEdit} onClick={() => void act(save)}><Save size={14} /> 保存草稿</button><button className="primary" disabled={busy || !canPublish} onClick={() => void preparePublish()}>校验并发布</button></>}</Heading>
    <Alert error={error} /><Alert>{notice}</Alert>
    {global && canEdit && canPublish && !draft && state.data?.active_revision !== revision.id && <div className="platform-actions"><button className="secondary" disabled={busy} onClick={() => void act(async () => { setRollback(true); setDiff(await platformFetch(`/configurations/${revision.id}/diff`)); })}>以此版本创建回滚发布</button></div>}
    {!draft && <section className="panel platform-panel"><h2>组件应用状态</h2>{applications.data?.items.map((item) => <p key={item.component}><b>{item.component}</b> <span className={`platform-state ${item.status}`}>{configStatus[item.status] ?? item.status}</span> {item.detail.instruction ?? item.detail.error_code}</p>)}<button className="secondary" disabled={busy || !canPublish} onClick={() => void act(async () => { await platformFetch(`/configurations/${revision.id}/check-application`, "POST"); await Promise.all([cache.invalidateQueries({ queryKey: ["platform"] }), cache.invalidateQueries({ queryKey: ["capabilities"] }), cache.invalidateQueries({ queryKey: ["model-catalog"] })]); })}>重新检查生效状态</button></section>}
    <nav className="platform-editor-nav" aria-label="配置分类">{groups.map(([key, label]) => <button key={key} aria-pressed={group === key} onClick={() => setGroup(key)}>{label}</button>)}{global && <>{[["quotas", "费用与配额"], ["services", "服务参数"], ["litellm", "模型路由"], ["sandbox_policy", "沙箱策略"], ["memory_backend", "记忆后端"], ["access", "接入与凭据引用"]].map(([key, label]) => <button key={key} aria-pressed={group === key} onClick={() => setGroup(key)}>{label}</button>)}</>}{!global && <button aria-pressed={group === "litellm"} onClick={() => setGroup("litellm")}>个人模型路由</button>}</nav>
    <section className="panel platform-panel"><div className="platform-form">
      {group === "quotas" ? <><h2>平台默认配额</h2><QuotaFields value={document.quotas} disabled={!editable} onChange={(quotas) => updateDocument({ ...document, quotas })} /></> : group === "services" ? <><h2>后台服务参数</h2><JsonInput label="服务配置（环境变量名称作为字段名）" value={document.services} disabled={!editable} onChange={(value) => updateDocument({ ...document, services: value as Record<string, unknown> })} onInvalid={(bad) => setInvalid((old) => ({ ...old, services: bad }))} /><details><summary>查看全部服务参数说明</summary>{Object.entries(catalog.services).filter(([, item]) => !item.bootstrap && !item.secret).map(([name, item]) => <p key={name}><code>{name}</code> · {item.description}</p>)}</details></> : group === "litellm" ? <><h2>模型路由与代理策略</h2><JsonInput label="LiteLLM 业务配置" value={document.litellm} disabled={!editable} onChange={(value) => updateDocument({ ...document, litellm: value as Record<string, unknown> })} onInvalid={(bad) => setInvalid((old) => ({ ...old, litellm: bad }))} /><p>密钥使用 credential:凭据编号 引用，或使用已绑定的 os.environ/变量名引用。</p></> : group === "sandbox_policy" ? <><h2>沙箱策略</h2><JsonInput label="沙箱策略对象" value={document.sandbox} disabled={!editable} onChange={(value) => updateDocument({ ...document, sandbox: value as Record<string, unknown> })} onInvalid={(bad) => setInvalid((old) => ({ ...old, sandbox: bad }))} /></> : group === "memory_backend" ? <><h2>OSS 记忆后端</h2><JsonInput label="记忆后端配置" value={document.memory} disabled={!editable} onChange={(value) => updateDocument({ ...document, memory: value as Record<string, unknown> })} onInvalid={(bad) => setInvalid((old) => ({ ...old, memory: bad }))} /><p>提供商密钥使用 credential:凭据编号 引用。</p></> : group === "access" ? <><h2>允许的接入与凭据引用</h2><label>开发者可用提供商（逗号分隔）<input disabled={!editable} value={document.allowed_providers.join(", ")} onChange={(event) => updateDocument({ ...document, allowed_providers: event.target.value.split(",").map((item) => item.trim()).filter(Boolean) })} /></label><label>开发者可用模型端点（每行一项）<textarea disabled={!editable} value={document.allowed_model_endpoints.join("\n")} onChange={(event) => updateDocument({ ...document, allowed_model_endpoints: event.target.value.split("\n").map((item) => item.trim()).filter(Boolean) })} /></label><JsonInput label="凭据绑定（变量名 → credential:凭据编号）" disabled={!editable} value={document.credential_refs} onChange={(value) => updateDocument({ ...document, credential_refs: value as Record<string, string> })} onInvalid={(bad) => setInvalid((old) => ({ ...old, refs: bad }))} /></> : <><label>搜索配置项<input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="按名称、用途或配置字段搜索" /></label>{fields.map((field) => {
        const value = document.runtime[field.name] ?? catalog.defaults[field.name];
        const rule = document.policy[field.name] ?? { editable: false };
        return <div className="platform-field" key={field.name}><div className="platform-field-header"><b>{field.label}</b><code>{field.name}</code></div><small>{field.description} {field.unit && `单位：${field.unit}。`}生效方式：{field.activation === "restart" ? "组件重载或重启后" : "新任务"}。</small><ValueControl field={field} value={value} disabled={!editable} onChange={(next) => updateDocument({ ...document, runtime: { ...document.runtime, [field.name]: next } })} onInvalid={(bad) => setInvalid((old) => ({ ...old, [field.name]: bad }))} />{global && <div className="platform-field-rule"><label><input type="checkbox" disabled={!editable} checked={rule.editable} onChange={(event) => updateDocument({ ...document, policy: { ...document.policy, [field.name]: { ...rule, editable: event.target.checked } } })} /> 允许用户调整</label>{rule.editable && typeof value === "number" && <><input type="number" aria-label={`${field.name} 最小值`} placeholder="最小值" disabled={!editable} value={rule.minimum ?? ""} onChange={(event) => updateDocument({ ...document, policy: { ...document.policy, [field.name]: { ...rule, minimum: event.target.value === "" ? null : Number(event.target.value) } } })} /><input type="number" aria-label={`${field.name} 最大值`} placeholder="最大值" disabled={!editable} value={rule.maximum ?? ""} onChange={(event) => updateDocument({ ...document, policy: { ...document.policy, [field.name]: { ...rule, maximum: event.target.value === "" ? null : Number(event.target.value) } } })} /></>}</div>}</div>;
      })}</>}
    </div></section>
    {diff && <PlatformModal title="确认并锁定配置版本" onClose={() => { if (!busy) setDiff(null); }}><p>发布后，此版本将保留。需要调整时可以复制为新草稿。</p><div className="platform-table-wrap"><table className="platform-table"><thead><tr><th>字段</th><th>原值</th><th>新值</th></tr></thead><tbody>{diff.changes.map((change) => <tr key={change.field}><td>{change.field}</td><td>{JSON.stringify(change.before)}</td><td>{JSON.stringify(change.after)}</td></tr>)}</tbody></table></div><div className="platform-form"><label>变更原因<input required value={reason} onChange={(event) => setReason(event.target.value)} placeholder="例如：更新默认研究模型与月度预算" /></label><Alert error={error} /><div className="platform-actions"><button className="primary" disabled={busy || !reason.trim()} onClick={() => void publish()}>确认锁定并发布</button><button className="secondary" disabled={busy} onClick={() => setDiff(null)}>返回编辑</button></div></div></PlatformModal>}
  </>;
}
