import { apiFetch } from "@/lib/api/http";

export type Source = { kind: "web" | "knowledge"; url: string | null; document_id: string | null; label: string };
export type PlanDefinition = {
  workspace_id: string; knowledge_base_id: string; name: string; objective: string; dimensions: string[];
  sources: Source[]; budget: { run_micro_usd: number; month_micro_usd: number; assessment_micro_usd: number };
  cadence: "manual" | "hourly" | "daily" | "weekly"; timezone: string; mode: "observe" | "auto";
  profile_revision_id: string | null; configurable: Record<string, unknown>; reviewer_ids: string[];
};
export type Snapshot = {
  id: string; metadata: { title: string; url: string; published_at?: string | null }; observed_at: string;
  content?: string; next_offset?: number | null; generation_id?: string | null;
};
export type Plan = {
  capabilities?: { manage: boolean; review: boolean; authorize: boolean };
  id: string; name: string; objective: string; workspace_id: string; knowledge_base_id: string; owner_id: string;
  version: number; status: string; mode: "observe" | "auto"; definition: PlanDefinition; reason?: string | null;
  baseline: { work_item_id?: string; fact_ids?: string[]; wiki_revision_ids?: string[] };
  sources?: { id: string; definition: Source; sequence: number; last_checked_at: string | null; next_run_at: string | null; error: string | null }[];
  budget_periods?: { period: string; reserved: number; settled: number }[];
};
export type Item = {
  capabilities?: { manage: boolean; review: boolean; authorize: boolean };
  id: string; plan_id: string; owner_id: string; version: number; question: string; status: string; quality_status: string;
  reason: string | null; run_id: string | null; create_time: string; manifest: { snapshots?: Snapshot[]; events?: { id: string; kind: string; previous_snapshot_id: string | null; snapshot_id: string | null; create_time: string }[] };
  result: { report?: string; runtime_status?: string; report_review?: { decision?: string; status?: string; issue_count?: number; attempt?: number; skipped?: boolean; degraded?: boolean } | null; pending_decision?: { stage: string; question: string } | null; evidence?: { evidence_id: string; claim: string; supporting_excerpt: string; source_url: string; observation_snapshot_id?: string }[] };
  review: { sources?: Record<string, { document_id: string; generation_id?: string }>; wiki_page_id?: string };
};
export type Notification = { id: string; kind: string; plan_id: string; item_id: string | null; read_at: string | null; delivery_status: string; create_time: string };
export type Options = { bases: { id: string; name: string; workspace_id: string; workspace_name: string; can_submit: boolean; can_review: boolean }[]; documents: { id: string; filename: string; knowledge_base_id: string }[]; members?: { user_id: string; name: string; knowledge_base_id: string; can_review: boolean }[]; facts?: { id: string; name: string; knowledge_base_id: string }[]; wiki_revisions?: { id: string; name: string; knowledge_base_id: string }[] };
export const endpoint = "/intelligence";
export const post = (body?: unknown): RequestInit => ({ method: "POST", headers: { "content-type": "application/json" }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
export const command = (version: number) => ({ expected_version: version, command_id: crypto.randomUUID() });
export const fetchPlans = (workspace: string) => apiFetch<{ items: Plan[] }>(`${endpoint}/plans${workspace ? `?workspace_id=${encodeURIComponent(workspace)}` : ""}`);
export const fetchItems = (plan?: string, workspace?: string) => apiFetch<{ items: Item[] }>(`${endpoint}/items?${new URLSearchParams({ ...(plan ? { plan_id: plan } : {}), ...(workspace ? { workspace_id: workspace } : {}) })}`);
export const money = (micro: number) => new Intl.NumberFormat("zh-CN", { style: "currency", currency: "USD", maximumFractionDigits: 4 }).format(micro / 1_000_000);
export const statusNames: Record<string, string> = { draft: "草稿", active: "已启用", paused: "已暂停", archived: "已归档", proposed: "待决定", queued: "排队中", running: "研究中", awaiting_review: "待审阅", completed: "已完成", blocked: "需要处理", cancelled: "已取消", dismissed: "已忽略" };
export const qualityNames: Record<string, string> = { passed: "质量验收通过", partial: "部分成果，尚未通过验收", failed: "质量验收未通过", unassessed: "尚未评估" };

export function errorText(error: unknown) {
  const raw = String(error);
  if (raw.includes("intelligence_disabled")) return "持续情报尚未启用。请由管理员完成迁移、启动情报 Worker，并发布平台配置。";
  if (raw.includes("requires_managed_accounting")) return "持续情报需要平台配置与个人费用配额，请管理员完成相关配置。";
  if (raw.includes("version_conflict")) return "这项内容已更新，请刷新后重新检查再操作。";
  if (raw.includes("feishu_not_configured")) return "管理员尚未配置飞书应用及 MarketScout 对外地址。";
  if (raw.includes("quality_not_passed")) return "研究尚未通过全部质量验收，暂不能发布为正式知识。";
  if (raw.includes("403")) return "当前账号没有执行此操作的权限，或长期授权已失效。";
  if (raw.includes("budget")) return "费用额度不足或尚未配置，请检查负责人和计划额度。";
  if (raw.includes("not_published") || raw.includes("published_knowledge_required")) return "请先完成引用资料与知识草稿的人工审阅发布。";
  if (raw.includes("excerpt_mismatch") || raw.includes("differs_from_observation")) return "已发布资料与原证据不一致，请重新核验引用后继续。";
  if (raw.includes("billing_owner_must_authorize")) return "请由计划负责人确认长期授权和费用上限。";
  if (raw.includes("feishu_api_rejected")) return "飞书未接受请求，请管理员核对应用权限与可用范围。";
  return "操作未完成，请刷新后重试。若问题持续，请查看站内异常记录或联系管理员。";
}
