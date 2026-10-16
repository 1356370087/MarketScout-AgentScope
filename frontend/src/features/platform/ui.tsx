"use client";
import { useState, type ReactNode } from "react";
import type { Quotas } from "@/lib/platform";
import { SurfaceDialog } from "@/components/ui/workspace";

export function PlatformModal({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  return <SurfaceDialog title={title} open onOpenChange={(open) => { if (!open) onClose(); }}><div className="platform-dialog">{children}</div></SurfaceDialog>;
}

export function WorkerState({ name, value }: { name: string; value: unknown }) {
  const state = (value ?? {}) as Record<string, unknown>;
  const disabled = state.enabled === false;
  const ready = state.ready === true || state.worker === "ready" && state.database === "ready";
  return <div className="platform-worker-state"><b>{name}</b><span className={`platform-state ${ready ? "active" : ""}`}>{disabled ? "未启用" : ready ? "就绪" : "未就绪"}</span><details><summary>诊断信息</summary><pre>{JSON.stringify(state, null, 2)}</pre></details></div>;
}

export function Heading({ title, description, children }: { title: string; description: string; children?: ReactNode }) {
  return <div className="platform-heading"><div><span className="platform-eyebrow">INSIGHTFORGE / PLATFORM</span><h1>{title}</h1><p>{description}</p></div>{children && <div className="platform-actions">{children}</div>}</div>;
}
export function Alert({ error, children }: { error?: unknown; children?: ReactNode }) {
  return error || children ? <div className={`platform-alert ${error ? "error" : ""}`} role={error ? "alert" : "status"}>{error instanceof Error ? error.message : error ? String(error) : children}</div> : null;
}
export function JsonInput({ label, value, onChange, onInvalid, disabled = false }: { label: string; value: unknown; onChange: (value: unknown) => void; onInvalid?: (invalid: boolean) => void; disabled?: boolean }) {
  const [text, setText] = useState(JSON.stringify(value ?? {}, null, 2));
  const [error, setError] = useState("");
  return <label className="platform-field"><span>{label}</span><textarea className="platform-json" disabled={disabled} value={text} onChange={(event) => { const next = event.target.value; setText(next); try { onChange(JSON.parse(next)); setError(""); onInvalid?.(false); } catch { setError("请输入有效的 JSON。"); onInvalid?.(true); } }} />{error && <small role="alert" className="platform-error">{error}</small>}</label>;
}
export const quotaLabels: Record<keyof Quotas, string> = { monthly_cost_micro_usd: "月度费用配额（美元）", max_run_cost_micro_usd: "单次研究费用上限（美元）", max_concurrent_runs_per_user: "同时运行的研究上限", api_run_create_per_minute: "每分钟创建研究上限", max_documents_per_user: "文档数量上限", max_bytes_per_user: "文档总容量（MiB）" };
export function QuotaFields({ value, onChange, disabled = false }: { value: Quotas; onChange: (value: Quotas) => void; disabled?: boolean }) {
  return <div className="platform-grid">{Object.entries(quotaLabels).map(([key, label]) => {
    const field = key as keyof Quotas;
    const scale = key.endsWith("micro_usd") ? 1_000_000 : key.endsWith("bytes_per_user") ? 1024 * 1024 : 1;
    return <label className="platform-field" key={key}><span>{label}</span><input type="number" min="0" step={scale === 1 ? "1" : "0.01"} disabled={disabled} value={value[field] == null ? "" : Number(value[field]) / scale} onChange={(event) => onChange({ ...value, [field]: event.target.value === "" ? null : Math.round(Number(event.target.value) * scale) })} /><small>{key === "monthly_cost_micro_usd" ? "包含已结算和在途预留费用；0 表示禁止新增付费调用。" : "用户覆盖项留空时沿用平台默认值。"}</small></label>;
  })}</div>;
}
