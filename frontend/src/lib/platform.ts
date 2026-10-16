import { authFetch } from "./auth";

export type FieldPolicy = { editable: boolean; minimum?: number | null; maximum?: number | null; allowed?: unknown[] | null };
export type Quotas = { monthly_cost_micro_usd?: number | null; max_run_cost_micro_usd?: number | null; max_concurrent_runs_per_user?: number | null; api_run_create_per_minute?: number | null; max_documents_per_user?: number | null; max_bytes_per_user?: number | null };
export type ConfigDocument = { schema_version: 1; runtime: Record<string, unknown>; services: Record<string, unknown>; credential_refs: Record<string, string>; litellm: Record<string, unknown>; sandbox: Record<string, unknown> | null; memory: Record<string, unknown>; policy: Record<string, FieldPolicy>; quotas: Quotas; allowed_providers: string[]; allowed_model_endpoints: string[] };
export type ConfigRevision = { id: string; profile_id: string; name: string; scope: "global" | "personal"; owner_id: string | null; number: number; version: number; status: string; created_at: string; fingerprint: string; document: ConfigDocument };
export type PlatformState = { initialized: boolean; active_revision: string | null; generation: number };
export type Preferences = { profile_revision_id: string | null; configurable: Record<string, unknown>; publication_theme: Record<string, unknown> };
export type ConfigPreview = { platform_revision_id: string; profile_revision_id: string; configurable: Record<string, unknown>; fingerprint: string; limits: Quotas };
export type Credential = { id: string; name: string; provider: string; owner_id: string | null; version: number; disabled: boolean };
export type CatalogField = { name: string; group: string; group_label: string; label: string; description: string; unit: string; bootstrap: boolean; secret: boolean; derived: boolean; activation: string; schema: Record<string, unknown> };
export type Catalog = { runtime: Record<string, CatalogField>; services: Record<string, { description: string; bootstrap: boolean; secret: boolean }>; defaults: Record<string, unknown>; policy: Record<string, FieldPolicy> };
export type Application = { component: string; status: string; detail: { instruction?: string; error_code?: string; mode?: string } };

export function platformFetch<T>(path: string, method = "GET", body?: unknown) {
  return authFetch<T>(`/api/platform${path}`, { method, ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }) });
}

export const platformApi = {
  status: () => platformFetch<PlatformState>("/status"),
  catalog: () => platformFetch<Catalog>("/catalog"),
  configurations: () => platformFetch<{ items: ConfigRevision[] }>("/configurations"),
  configuration: (id: string) => platformFetch<ConfigRevision>(`/configurations/${encodeURIComponent(id)}`),
  preferences: () => platformFetch<Preferences>("/preferences"),
  savePreferences: (value: Preferences) => platformFetch<Preferences>("/preferences", "PUT", value),
  preview: (profile_revision_id: string | null, configurable: Record<string, unknown>) => platformFetch<ConfigPreview>("/configuration-preview", "POST", { profile_revision_id, configurable }),
  credentials: () => platformFetch<{ items: Credential[] }>("/credentials"),
};

export const configStatus: Record<string, string> = { draft: "草稿", pending: "待应用", active: "已生效", failed: "应用失败", applied: "已应用" };
export function canOpenAdmin(permissions: string[] = []) {
  return permissions.some((item) => item.startsWith("platform.") || item.startsWith("iam.users.") || item.startsWith("iam.roles.") || item === "iam.audit.read");
}
export function workspaceReturnPath(value: string | null) {
  return value && /^\/(research(?:\/|$)|documents(?:\/|$)|knowledge(?:\/|$)|usage(?:\?|$)|settings(?:\?|$)|account\/)/.test(value) && !value.startsWith("//") ? value : "/research/new";
}
export const money = (micro: unknown) => `$${(Number(micro ?? 0) / 1_000_000).toFixed(4)}`;

export function emptyDocument(): ConfigDocument {
  return { schema_version: 1, runtime: {}, services: {}, credential_refs: {}, litellm: {}, sandbox: null, memory: {}, policy: {}, quotas: {}, allowed_providers: ["openai", "anthropic", "deepseek", "gemini", "zhipu", "dashscope", "tavily", "brave", "bing"], allowed_model_endpoints: [] };
}
