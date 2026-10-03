import { ChevronDown, Check, Search } from "lucide-react";
import type { ReactNode } from "react";
import "./insight.css";

export function MetricStrip({ items, label }: { items: { label: string; value: ReactNode; icon?: ReactNode }[]; label: string }) {
  return <dl className="insight-metrics" aria-label={label}>{items.map((item) => <div key={item.label}><dt>{item.icon}{item.label}</dt><dd>{item.value ?? "未提供"}</dd></div>)}</dl>;
}

export function Disclosure({ title, meta, children, open }: { title: ReactNode; meta?: ReactNode; children: ReactNode; open?: boolean }) {
  return <details className="insight-disclosure" open={open}><summary><span>{title}</span>{meta && <small>{meta}</small>}<ChevronDown size={16} aria-hidden /></summary><div className="insight-disclosure-body">{children}</div></details>;
}

export function LoadingSkeleton({ label, rows = 3 }: { label: string; rows?: number }) {
  return <div className="insight-loading" role="status"><span>{label}</span><div aria-hidden>{Array.from({ length: rows }, (_, index) => <div className="insight-skeleton" key={index} />)}</div></div>;
}

export function SearchField({ label, value, onChange, placeholder }: { label: string; value: string; onChange: (value: string) => void; placeholder?: string }) {
  return <label className="insight-search"><Search size={16} aria-hidden /><input type="search" aria-label={label} placeholder={placeholder ?? label} value={value} onChange={(event) => onChange(event.target.value)} /></label>;
}

export function SelectionSummary({ count, children, onClear }: { count: number; children: ReactNode; onClear: () => void }) {
  return <div className="insight-selection"><span><Check size={16} aria-hidden />已选择 {count} 项</span><div className="ui-actions"><button type="button" className="ui-text-button" onClick={onClear}>清空选择</button>{children}</div></div>;
}

const fieldNames: Record<string, string> = {
  title: "标题", subject: "主题", description: "说明", objective: "目标", objectives: "研究目标", goal: "目标",
  steps: "执行步骤", tasks: "研究任务", queries: "检索问题", sources: "来源", methodology: "研究方法",
  expected_output: "预期产出", expected_outcome: "预期结果", deliverables: "交付内容", scope: "研究范围",
  requirements: "需求", dependencies: "前置依赖", rationale: "理由", reason: "原因", summary: "摘要",
  content: "内容", message: "消息", plan: "计划", research_plan: "研究计划", criteria: "判断标准",
  missing_information: "待补充信息", follow_up_tasks: "后续任务", hard_rejection_reasons: "未接纳原因",
};

/** Present already-public structured records without turning them into raw JSON. */
export function StructuredContent({ value }: { value: unknown }) {
  if (value == null || value === "") return <span className="insight-muted">未提供</span>;
  if (Array.isArray(value)) return value.length ? <ul className="insight-record-list">{value.map((item, index) => <li key={index}><StructuredContent value={item} /></li>)}</ul> : <span className="insight-muted">无</span>;
  if (typeof value === "object") return <dl className="insight-record">{Object.entries(value).map(([key, item]) => <div key={key}><dt>{fieldNames[key] ?? key.replaceAll("_", " ")}</dt><dd><StructuredContent value={item} /></dd></div>)}</dl>;
  return <span className="insight-record-text">{typeof value === "boolean" ? value ? "是" : "否" : String(value)}</span>;
}
