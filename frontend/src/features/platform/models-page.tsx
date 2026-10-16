"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { iamApi } from "@/lib/auth";
import { platformApi, platformFetch, money, type Credential, type ConfigRevision } from "@/lib/platform";
import { Heading, Alert, PlatformModal } from "./ui";

type Model = { name: string; deployment: string; scope: string; revision_id: string; provider_model: string; model_info: Record<string, unknown> };
export default function ModelsPage() {
  const cache = useQueryClient();
  const router = useRouter();
  const identity = useQuery({ queryKey: ["identity"], queryFn: iamApi.me });
  const credentials = useQuery({ queryKey: ["platform", "credentials"], queryFn: platformApi.credentials });
  const models = useQuery({ queryKey: ["platform", "models"], queryFn: () => platformFetch<{ items: Model[] }>("/models") });
  const admin = identity.data?.permissions.includes("platform.credentials.manage");
  const [mode, setMode] = useState<"credential" | "model" | null>(null);
  const [replace, setReplace] = useState<Credential | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const [notice, setNotice] = useState("");
  async function action(run: () => Promise<void>) { setBusy(true); setError(undefined); try { await run(); await cache.invalidateQueries({ queryKey: ["platform"] }); } catch (cause) { setError(cause); } finally { setBusy(false); } }
  async function save(form: FormData) {
    await action(async () => {
      const value = Object.fromEntries(form);
      if (replace) { await platformFetch(`/credentials/${replace.id}`, "PUT", { secret: value.secret, expected_version: replace.version }); setReplace(null); }
      else if (mode === "credential") { await platformFetch("/credentials", "POST", { name: value.name, provider: value.provider, scope: value.scope ?? "personal", secret: value.secret }); setMode(null); }
      else {
        const result = await platformFetch<ConfigRevision>("/models", "POST", { name: String(value.name) + " · 模型配置", scope: value.scope ?? "personal", model: { model_name: value.name,
          litellm_params: { model: value.model, api_key: "credential:" + value.credential, ...(value.endpoint ? { api_base: value.endpoint } : {}) },
          model_info: { base_model: value.model, max_input_tokens: Number(value.context), max_output_tokens: Number(value.output), input_cost_per_token: Number(value.input_price) / 1_000_000, output_cost_per_token: Number(value.output_price) / 1_000_000 } } });
        router.push(`/admin/configurations/${result.id}`);
      }
    });
  }
  return <><Heading title="模型与凭据" description="维护平台模型，或添加仅供自己使用的调试接入。"><button className="secondary" onClick={() => setMode("credential")}>添加凭据</button><button className="primary" onClick={() => setMode("model")}>添加模型</button></Heading><Alert error={error || credentials.error || models.error} /><Alert>{notice}</Alert>
    <section className="panel platform-panel"><h2>可用模型</h2><p>连接测试会调用模型，并计入你的月度额度。</p><div className="platform-table-wrap"><table className="platform-table"><thead><tr><th>模型</th><th>范围</th><th>能力</th><th>每百万 Token 价格</th><th>操作</th></tr></thead><tbody>{models.data?.items.map((model) => <tr key={`${model.revision_id}:${model.name}`}><td><b>{model.name}</b><small>{model.provider_model}</small></td><td>{model.scope === "global" ? "平台" : "个人"}</td><td>上下文 {String(model.model_info.max_input_tokens ?? "—")}<small>输出 {String(model.model_info.max_output_tokens ?? "—")}</small></td><td>输入 ${Number(model.model_info.input_cost_per_token ?? 0) * 1_000_000}<small>输出 ${Number(model.model_info.output_cost_per_token ?? 0) * 1_000_000}</small></td><td><button className="secondary" disabled={busy} onClick={() => void action(async () => { const result = await platformFetch<{ duration_ms: number; cost_micro_usd: number }>("/models/test", "POST", { revision_id: model.revision_id, model_name: model.name }); setNotice(`连接成功，耗时 ${result.duration_ms} ms，费用 ${money(result.cost_micro_usd)}。`); })}>测试连接</button></td></tr>)}</tbody></table>{models.isSuccess && !models.data.items.length && <div className="platform-empty">配置发布并完成应用后，模型会显示在这里。</div>}</div></section>
    <section className="panel platform-panel"><h2>凭据目录</h2><div className="platform-table-wrap"><table className="platform-table"><thead><tr><th>名称</th><th>提供商</th><th>归属</th><th>状态</th><th>操作</th></tr></thead><tbody>{credentials.data?.items.map((credential) => <tr key={credential.id}><td>{credential.name}<small>{credential.id}</small></td><td>{credential.provider}</td><td>{credential.owner_id ? "个人" : "平台"}</td><td><span className={`platform-state ${credential.disabled ? "failed" : "active"}`}>{credential.disabled ? "已停用" : `有效 · v${credential.version}`}</span></td><td>{(credential.owner_id === identity.data?.id || !credential.owner_id && admin) && <div className="platform-actions"><button className="secondary" onClick={() => setReplace(credential)}>替换</button><button className="danger" disabled={credential.disabled || busy} onClick={() => { if (window.confirm("停用后，引用此凭据的后续调用将被阻止。确认停用？")) void action(async () => { await platformFetch(`/credentials/${credential.id}/disable`, "POST"); }); }}>停用</button></div>}</td></tr>)}</tbody></table></div></section>
    {(mode || replace) && <PlatformModal title={replace ? `替换凭据：${replace.name}` : mode === "credential" ? "添加凭据" : "添加模型配置"} onClose={() => { if (!busy) { setMode(null); setReplace(null); }; }}><form action={save} className="platform-form">{!replace && <><label>名称<input name="name" required maxLength={160} /></label>{admin && <label>归属范围<select name="scope"><option value="global">平台</option><option value="personal">个人</option></select></label>}</>}{mode === "credential" || replace ? <>{!replace && <label>提供商<select name="provider">{["openai", "anthropic", "deepseek", "gemini", "zhipu", "dashscope", "tavily", "brave", "bing"].map((provider) => <option key={provider}>{provider}</option>)}</select></label>}<label>API Key<input name="secret" type="password" autoComplete="new-password" required /><small>加密保存，保存后仅显示凭据状态。</small></label></> : <><label>提供商与模型<input name="model" placeholder="openai/模型名称" required /></label><label>服务端点<input name="endpoint" type="url" placeholder="https://服务地址/v1（可选）" /></label><label>凭据<select name="credential" required><option value="">选择有效凭据</option>{credentials.data?.items.filter((item) => !item.disabled).map((item) => <option key={item.id} value={item.id}>{item.name} · {item.owner_id ? "个人" : "平台"}</option>)}</select></label><div className="platform-grid"><label>上下文 Token 上限<input name="context" type="number" min={1} required /></label><label>输出 Token 上限<input name="output" type="number" min={1} required /></label><label>输入价格（美元 / 百万 Token）<input name="input_price" type="number" min={0} step="any" required /></label><label>输出价格（美元 / 百万 Token）<input name="output_price" type="number" min={0} step="any" required /></label></div></>}<Alert error={error} /><div className="platform-actions"><button className="primary" disabled={busy}>{busy ? "正在保存…" : mode === "model" ? "保存为配置草稿" : "加密保存"}</button><button type="button" className="secondary" onClick={() => { setMode(null); setReplace(null); }}>取消</button></div></form></PlatformModal>}
  </>;
}
