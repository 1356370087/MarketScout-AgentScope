"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Plus, Upload } from "lucide-react";
import { iamApi } from "@/lib/auth";
import { configStatus, emptyDocument, platformApi, platformFetch, type ConfigRevision, type ConfigDocument } from "@/lib/platform";
import { Heading, Alert, PlatformModal } from "./ui";
import { LoadingSkeleton } from "@/components/ui/insight";

export default function ConfigurationsPage() {
  const router = useRouter();
  const cache = useQueryClient();
  const list = useQuery({ queryKey: ["platform", "configurations"], queryFn: platformApi.configurations });
  const status = useQuery({ queryKey: ["platform", "status"], queryFn: platformApi.status });
  const identity = useQuery({ queryKey: ["identity"], queryFn: iamApi.me });
  const admin = identity.data?.permissions.includes("platform.config.write");
  const [error, setError] = useState<unknown>();
  const [importing, setImporting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [format, setFormat] = useState("env");
  const [content, setContent] = useState("");
  const [name, setName] = useState("我的研究配置");
  const [scope, setScope] = useState("global");
  const [preview, setPreview] = useState<{ document: ConfigDocument; report: { deployment_fields: string[]; unsupported_fields: string[]; credential_count: number } } | null>(null);
  const payload = { name, scope: admin ? scope : "personal", format, content };
  async function create() {
    setBusy(true); setError(undefined);
    try { const draft = await platformFetch<ConfigRevision>("/configurations", "POST", { name, scope: admin ? "global" : "personal", document: emptyDocument() }); await cache.invalidateQueries({ queryKey: ["platform"] }); router.push(`/admin/configurations/${draft.id}`); }
    catch (cause) { setError(cause); } finally { setBusy(false); }
  }
  async function previewImport() { setBusy(true); setError(undefined); try { setPreview(await platformFetch("/import-preview", "POST", payload)); } catch (cause) { setError(cause); } finally { setBusy(false); } }
  async function confirmImport() { setBusy(true); setError(undefined); try { const draft = await platformFetch<ConfigRevision>("/import", "POST", payload); setContent(""); await cache.invalidateQueries({ queryKey: ["platform"] }); router.push(`/admin/configurations/${draft.id}`); } catch (cause) { setError(cause); } finally { setBusy(false); } }
  return <><Heading title="配置中心" description="编写一次，确认锁定后持续复用。每次研究记录使用的配置版本。"><button className="secondary" onClick={() => setImporting(true)}><Upload size={14} /> 导入已有配置</button><button className="primary" disabled={busy} onClick={() => void create()}><Plus size={14} /> 新建{admin ? "平台" : "个人"}配置</button></Heading>
    {!status.data?.active_revision && <section className="panel platform-panel"><h2>完成平台首次配置</h2><p>发布并确认组件就绪后，即可开始研究。</p><div className="platform-steps"><span className="active">1 导入或创建配置</span><span>2 填写接入与预算</span><span>3 校验并发布</span><span>4 检查生效状态</span></div></section>}
    <Alert error={error || list.error} />
    <section className="panel platform-panel"><div className="platform-table-wrap">{list.isPending ? <LoadingSkeleton label="正在读取配置版本…" /> : <table className="platform-table"><thead><tr><th>配置名称</th><th>范围</th><th>版本</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead><tbody>{list.data?.items.map((item) => <tr key={item.id}><td><Link href={`/admin/configurations/${item.id}`}>{item.name}</Link>{status.data?.active_revision === item.id && <small>当前平台默认配置</small>}</td><td>{item.scope === "global" ? "平台" : "个人"}</td><td>v{item.number}</td><td><span className={`platform-state ${item.status}`}>{configStatus[item.status] ?? item.status}</span></td><td>{new Date(item.created_at).toLocaleString("zh-CN")}</td><td><Link href={`/admin/configurations/${item.id}`}>查看配置</Link></td></tr>)}</tbody></table>}{list.isSuccess && !list.data.items.length && <div className="platform-empty">还没有配置。从现有配置导入，或创建第一份草稿。</div>}</div></section>
    {importing && <PlatformModal title="导入配置" onClose={() => { if (!busy) { setImporting(false); setContent(""); setPreview(null); }; }}><p>先预览字段与凭据，再将结果保存为草稿。</p><div className="platform-form"><label>配置名称<input value={name} onChange={(event) => setName(event.target.value)} /></label>{admin && <label>保存范围<select value={scope} onChange={(event) => { setScope(event.target.value); setPreview(null); }}><option value="global">平台配置</option><option value="personal">个人调试配置</option></select></label>}<label>配置格式<select value={format} onChange={(event) => { setFormat(event.target.value); setPreview(null); }}><option value="env">环境配置 .env</option><option value="yaml">LiteLLM YAML</option><option value="toml">沙箱 TOML</option><option value="json">配置导出包 JSON</option>{admin && <option value="server">服务器当前配置</option>}</select></label>{format !== "server" && <><label>选择文件<input type="file" accept=".env,.yaml,.yml,.toml,.json,.txt" onChange={(event) => { const file = event.target.files?.[0]; if (file) void file.text().then((text) => { setContent(text); setPreview(null); }); }} /></label><label>或粘贴配置<textarea className="platform-json" value={content} onChange={(event) => { setContent(event.target.value); setPreview(null); }} /></label></>}
      {preview && <div className="platform-alert"><b>导入预览</b><p>研究参数 {Object.keys(preview.document.runtime).length} 项 · 服务参数 {Object.keys(preview.document.services).length} 项 · 待加密凭据 {preview.report.credential_count} 项</p><p>保留在部署环境：{preview.report.deployment_fields.join("、") || "无"}</p><p>未识别字段：{preview.report.unsupported_fields.join("、") || "无"}</p></div>}<Alert error={error} /><div className="platform-actions"><button className="primary" disabled={busy} onClick={() => void (preview ? confirmImport() : previewImport())}>{busy ? "正在处理…" : preview ? "确认导入为草稿" : "预览导入结果"}</button><button className="secondary" disabled={busy} onClick={() => { setImporting(false); setContent(""); setPreview(null); }}>取消</button></div></div></PlatformModal>}
  </>;
}
