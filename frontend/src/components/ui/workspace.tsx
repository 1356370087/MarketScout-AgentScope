"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { Check, ChevronsUpDown, Inbox, Search, X } from "lucide-react";
import { useId, useRef, useState, type ReactNode } from "react";

export function PageHeading({ title, description, actions, eyebrow }: { title: string; description?: string; actions?: ReactNode; eyebrow?: string }) {
  return <header className="page-header"><div>{eyebrow && <span className="eyebrow">{eyebrow}</span>}<h1>{title}</h1>{description && <p>{description}</p>}</div>{actions && <div className="heading-actions">{actions}</div>}</header>;
}

export function EmptyState({ title, description, action }: { title: string; description?: string; action?: ReactNode }) {
  return <div className="ui-empty"><Inbox size={28} aria-hidden="true" /><h3>{title}</h3>{description && <p>{description}</p>}{action}</div>;
}

const labels: Record<string, string> = {
  pending: "等待中", queued: "排队中", running: "进行中", active: "协作中", completed: "已完成", failed: "失败", cancelled: "已取消", cancelling: "正在停止",
  waiting_for_confirmation: "等待确认", ready: "可检索", processing: "处理中", deleting: "正在清理", accepted: "已接纳", rejected: "需补证", closed: "已关闭",
  awaiting_clarification: "待澄清", awaiting_plan_approval: "待确认计划", awaiting_outline_approval: "待确认大纲", awaiting_fetch_budget_approval: "待确认预算",
};
export function StatusBadge({ status, label }: { status: string; label?: string }) {
  const tone = ["completed", "ready", "accepted"].includes(status) ? "success" : ["failed", "rejected"].includes(status) ? "error" : status.startsWith("awaiting") || status === "waiting_for_confirmation" ? "warning" : ["running", "active", "processing"].includes(status) ? "active" : "neutral";
  return <span className={`ui-status ${tone}`}><i aria-hidden="true" />{label ?? labels[status] ?? status}</span>;
}

export interface TabItem { value: string; label: ReactNode; disabled?: boolean }
export function Tabs({ label, value, onChange, items, children }: { label: string; value: string; onChange: (value: string) => void; items: TabItem[]; children: ReactNode }) {
  const id = useId();
  const refs = useRef<Array<HTMLButtonElement | null>>([]);
  const focusableValue = items.find((item) => item.value === value && !item.disabled)?.value ?? items.find((item) => !item.disabled)?.value;
  return <div className="ui-tabs"><div role="tablist" aria-label={label} className="ui-tab-list">{items.map((item, index) => <button key={item.value} ref={(node) => { refs.current[index] = node; }} type="button" role="tab" id={`${id}-${item.value}`} aria-controls={`${id}-panel`} aria-selected={value === item.value} tabIndex={focusableValue === item.value ? 0 : -1} disabled={item.disabled} onClick={() => onChange(item.value)} onKeyDown={(event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const enabled = items.map((entry, i) => entry.disabled ? -1 : i).filter((i) => i !== -1);
    const current = enabled.indexOf(index);
    const next = event.key === "Home" ? enabled[0] : event.key === "End" ? enabled.at(-1)! : enabled[(current + (event.key === "ArrowRight" ? 1 : -1) + enabled.length) % enabled.length];
    onChange(items[next].value); refs.current[next]?.focus();
  }}>{item.label}</button>)}</div><div id={`${id}-panel`} role="tabpanel" aria-labelledby={`${id}-${value}`}>{children}</div></div>;
}

export function ChoiceGroup({ label, value, options, onChange, disabled = false }: { label: string; value: string; options: Array<{ value: string; label: string; description?: string; disabled?: boolean }>; onChange: (value: string) => void; disabled?: boolean }) {
  const id = useId();
  return <fieldset className="ui-choices" disabled={disabled}><legend>{label}</legend>{options.map((option) => <label key={option.value} className={value === option.value ? "selected" : ""}><input type="radio" name={id} value={option.value} checked={value === option.value} disabled={option.disabled} onChange={() => onChange(option.value)} /><span><b>{option.label}</b>{option.description && <small>{option.description}</small>}</span></label>)}</fieldset>;
}

export function SurfaceDialog({ title, description, trigger, children, open, onOpenChange, onCloseAutoFocus, drawer = false }: { title: string; description?: string; trigger?: ReactNode; children: ReactNode; open?: boolean; onOpenChange?: (open: boolean) => void; onCloseAutoFocus?: (event: Event) => void; drawer?: boolean }) {
  const returnFocus = useRef<HTMLElement | null>(null);
  return <Dialog.Root open={open} onOpenChange={onOpenChange}>{trigger && <Dialog.Trigger asChild>{trigger}</Dialog.Trigger>}<Dialog.Portal><Dialog.Overlay className="ui-overlay" /><Dialog.Content className={`ui-dialog ${drawer ? "ui-drawer" : ""}`} {...(!description ? { "aria-describedby": undefined } : {})} onOpenAutoFocus={() => { returnFocus.current = document.activeElement as HTMLElement | null; }} onCloseAutoFocus={(event) => { onCloseAutoFocus?.(event); if (event.defaultPrevented) return; if (!trigger && returnFocus.current?.isConnected) { event.preventDefault(); returnFocus.current.focus(); } }}><header><div><Dialog.Title>{title}</Dialog.Title>{description && <Dialog.Description>{description}</Dialog.Description>}</div><Dialog.Close className="ui-icon" aria-label="关闭"><X size={18} /></Dialog.Close></header><div className="ui-dialog-body">{children}</div></Dialog.Content></Dialog.Portal></Dialog.Root>;
}

export function SearchSelect({ id, label, value, options, onChange, disabled }: { id?: string; label: string; value: string; options: Array<{ value: string; label: string }>; onChange: (value: string) => void; disabled?: boolean }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const filtered = options.filter((option) => `${option.label} ${option.value}`.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  return <SurfaceDialog title={label} description="搜索并选择一个选项。" open={open} onOpenChange={(next) => { setOpen(next); if (!next) setQuery(""); }} trigger={<button id={id} type="button" className="ui-select" aria-label={label} disabled={disabled}><span>{options.find((option) => option.value === value)?.label ?? (value || "请选择")}</span><ChevronsUpDown size={16} /></button>}>
    <label className="ui-search"><Search size={16} /><input aria-label={`搜索${label}`} placeholder="输入名称搜索…" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
    <div className="ui-option-list">{filtered.map((option) => <button type="button" key={option.value} aria-pressed={option.value === value} onClick={() => { onChange(option.value); setOpen(false); setQuery(""); }}><span>{option.label}</span>{option.value === value && <Check size={16} />}</button>)}{!filtered.length && <EmptyState title="没有匹配的选项" description="试试其他关键词。" />}</div>
  </SurfaceDialog>;
}
