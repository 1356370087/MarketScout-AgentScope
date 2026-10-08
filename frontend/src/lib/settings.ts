import { z } from "zod";
import type { PublicationTheme } from "./types";

export const SETTINGS_KEY = "odr.frontend.settings.v1";
export const PRESETS_KEY = "odr.frontend.presets.v1";
export const PUBLICATION_THEME_KEY = "odr.frontend.publication-theme.v1";
export const WEB_SEARCH_PROVIDERS = ["tavily", "openai", "anthropic", "bing", "brave"] as const;

/** A null selection inherits the legacy setting; [] explicitly disables discovery. */
export function effectiveSearchProviders(value: unknown, legacy: unknown = "tavily"): string[] {
  if (Array.isArray(value)) return value.filter((item): item is string => typeof item === "string" && WEB_SEARCH_PROVIDERS.includes(item as typeof WEB_SEARCH_PROVIDERS[number]));
  return typeof legacy === "string" && WEB_SEARCH_PROVIDERS.includes(legacy as typeof WEB_SEARCH_PROVIDERS[number]) ? [legacy] : [];
}

export const defaultPublicationTheme: PublicationTheme = {
  preset: "default",
  primary_color: "#0F766E",
  accent_color: "#F6BD60",
  font_family: "cjk_sans",
  locale: "zh-CN",
  footer_text: "",
  pdf_page_size: "a4",
  pptx_aspect_ratio: "16:9",
};

export function applyPublicationThemePreset(theme: PublicationTheme, preset: PublicationTheme["preset"]): PublicationTheme {
  const values = {
    default: { primary_color: "#0F766E", accent_color: "#F6BD60", font_family: "cjk_sans" as const },
    boardroom: { primary_color: "#1F4E79", accent_color: "#C9A227", font_family: "cjk_sans" as const },
    academic: { primary_color: "#333333", accent_color: "#8B1E3F", font_family: "serif" as const },
  }[preset];
  return { ...theme, preset, ...values };
}

export const settingGroups: Array<{ id: string; label: string; keys: string[] }> = [
  { id: "basic", label: "研究流程", keys: ["allow_clarification", "enable_async_research", "async_research_mode", "team_execution_mode", "report_time_reserve_ratio"] },
  { id: "efficiency", label: "研究效率与运行预算", keys: ["research_efficiency_mode", "max_supplement_rounds", "max_no_progress_rounds", "research_context_target_tokens", "handoff_context_target_tokens", "run_deadline_seconds", "max_run_model_calls", "max_run_tool_calls", "max_run_input_tokens", "max_run_output_tokens", "max_run_cost_micro_usd", "model_call_timeout_seconds", "tool_call_timeout_seconds", "research_tool_call_timeout_seconds", "task_timeout_seconds"] },
  { id: "models", label: "模型与 Token 预算", keys: ["summarization_model", "summarization_model_max_tokens", "research_model", "research_model_max_tokens", "compression_model", "compression_model_max_tokens", "final_report_model", "final_report_model_max_tokens"] },
  { id: "evidence", label: "来源与证据", keys: ["search_providers", "search_max_concurrency", "openai_search_model", "anthropic_search_model", "knowledge_rerank_model", "web_pipeline_mode", "web_pipeline_shadow_sample_rate", "web_shadow_fetch_top_k", "web_shadow_timeout_seconds", "web_min_source_authority", "search_candidate_limit", "max_fetches_per_researcher"] },
  { id: "agents", label: "并行研究团队", keys: ["max_concurrent_research_units", "max_researcher_iterations", "max_react_tool_calls"] },
  { id: "hitl", label: "人工决策点", keys: ["enable_human_in_loop", "hitl_require_plan_approval", "hitl_require_outline_approval", "hitl_max_plan_revisions", "hitl_feedback_mode"] },
  { id: "report", label: "报告与交付", keys: ["report_type", "output_format"] },
  { id: "quality", label: "质量门禁与复核", keys: ["quality_evaluation_enabled", "quality_evaluation_model", "quality_evaluation_model_max_tokens", "quality_evaluation_rigor", "quality_evaluation_min_sources", "quality_evaluation_max_input_chars", "quality_risk_mode", "quality_evaluation_fail_open", "quality_caveat_admission_enabled", "quality_gap_recovery_max_attempts", "report_review_enabled", "report_review_model", "report_review_model_max_tokens", "report_review_temperature", "report_review_max_input_chars", "report_review_max_revisions", "report_review_fail_open"] },
  { id: "memory", label: "组织研究记忆", keys: ["enable_memory", "memory_top_k", "memory_min_confidence", "memory_auto_write", "memory_write_after_report", "memory_fail_open", "memory_advanced_enabled", "memory_decay_enabled", "memory_reflection_enabled", "memory_profile_enabled", "memory_soft_forgetting_enabled", "memory_verified_insights_enabled", "memory_search_threshold", "memory_search_rerank", "memory_importance_weight", "memory_relevance_weight", "memory_recency_weight", "memory_reflection_observation_threshold", "memory_reflection_importance_threshold", "memory_reflection_max_age_hours", "memory_profile_max_chars"] },
];

const objectSchema = z.record(z.string(), z.unknown());

export function loadSettings(defaults: Record<string, unknown> = {}) {
  if (typeof window === "undefined") return defaults;
  try { return { ...defaults, ...objectSchema.parse(JSON.parse(localStorage.getItem(SETTINGS_KEY) ?? "{}")) }; }
  catch { return defaults; }
}

export type SettingSchema = {
  type?: string;
  minimum?: number;
  exclusiveMinimum?: number;
  maximum?: number;
  anyOf?: Array<{ type?: string; minimum?: number; exclusiveMinimum?: number; maximum?: number; enum?: unknown[] }>;
};

/** Resolve a field type when JSON Schema represents a nullable value with anyOf. */
export function settingFieldType(schema: SettingSchema, value: unknown): string {
  if (schema.type) return schema.type;
  const variantType = schema.anyOf?.find((variant) => variant.type && variant.type !== "null")?.type;
  if (variantType) return variantType;
  return typeof value === "boolean" ? "boolean" : typeof value === "number" ? "number" : "string";
}

/** Resolve numeric bounds from either the field or its nullable numeric variant. */
export function settingFieldBounds(schema: SettingSchema, value: unknown): { minimum: number; maximum: number } {
  const type = settingFieldType(schema, value);
  const numericVariant = schema.anyOf?.find((variant) => variant.type === "number" || variant.type === "integer");
  const exclusiveMinimum = schema.exclusiveMinimum ?? numericVariant?.exclusiveMinimum;
  const minimum = schema.minimum ?? numericVariant?.minimum ?? (exclusiveMinimum === undefined ? 0 : exclusiveMinimum + (type === "integer" ? 1 : 0.01));
  const maximum = schema.maximum ?? numericVariant?.maximum ?? Math.max(Number(value || 100) * 2, 100);
  // A malformed schema should not make the range input unusable.
  return {
    minimum: Number.isFinite(minimum) ? minimum : 0,
    maximum: Number.isFinite(maximum) && maximum >= minimum ? maximum : (type === "integer" ? Math.max(minimum, 100) : Math.max(minimum, 1)),
  };
}

/** Add an explicit unset choice for nullable model settings. */
export function modelChoicesFor(
  schema: SettingSchema,
  value: unknown,
  options: string[],
): string[] {
  const current = String(value ?? "");
  const nullable = schema.anyOf?.some((variant) => variant.type === "null") ?? false;
  const choices = current && !options.includes(current) ? [current, ...options] : [...options];
  if (nullable) {
    // Keep exactly one explicit empty value at the front so a nullable model can
    // opt back into its server-side fallback alias.
    return ["", ...choices.filter((choice) => choice !== "")];
  }
  return choices;
}

export function sanitizeSettings(value: unknown, capabilities: { editable_config_keys?: string[]; config_schema?: { properties?: Record<string, SettingSchema> } }) {
  const raw = objectSchema.parse(value);
  const allowed = new Set(capabilities.editable_config_keys ?? []);
  const properties = capabilities.config_schema?.properties ?? {};
  return Object.fromEntries(Object.entries(raw).filter(([key, item]) => {
    if (!allowed.has(key)) return false;
    const rule = properties[key];
    if (!rule) return true;
    const variants = rule.anyOf ?? [rule];
    // Nullable Pydantic fields are represented as anyOf[type, null].  Null is
    // valid only when the schema explicitly advertises that variant.
    if (item === null) return variants.some((variant) => variant.type === "null");
    const matching = variants.filter((variant) => !variant.type || variant.type === typeof item || (variant.type === "array" && Array.isArray(item)) || (variant.type === "integer" && typeof item === "number" && Number.isInteger(item)));
    if (matching.length === 0) return false;
    if (key === "search_providers") return Array.isArray(item) && item.every((provider) => WEB_SEARCH_PROVIDERS.includes(provider));
    if (typeof item === "number") {
      if (!Number.isFinite(item)) return false;
      const bounded = matching.find((variant) => variant.type === "number" || variant.type === "integer") ?? rule;
      if (bounded.minimum !== undefined && item < bounded.minimum) return false;
      if (bounded.exclusiveMinimum !== undefined && item <= bounded.exclusiveMinimum) return false;
      if (bounded.maximum !== undefined && item > bounded.maximum) return false;
      if (bounded.type === "integer" && !Number.isInteger(item)) return false;
    }
    return true;
  }));
}

export function saveSettings(value: Record<string, unknown>) { localStorage.setItem(SETTINGS_KEY, JSON.stringify(value)); }

export function sanitizePublicationTheme(value: unknown, defaults: PublicationTheme = defaultPublicationTheme): PublicationTheme {
  const raw = objectSchema.safeParse(value);
  if (!raw.success) return defaults;
  const candidate = raw.data;
  const color = (key: string, fallback: string) => typeof candidate[key] === "string" && /^#[0-9a-fA-F]{6}$/.test(candidate[key]) ? candidate[key].toUpperCase() : fallback;
  const pick = <T extends string>(key: string, choices: readonly T[], fallback: T): T => choices.includes(candidate[key] as T) ? candidate[key] as T : fallback;
  return {
    preset: pick("preset", ["default", "boardroom", "academic"] as const, defaults.preset),
    primary_color: color("primary_color", defaults.primary_color),
    accent_color: color("accent_color", defaults.accent_color),
    font_family: pick("font_family", ["cjk_sans", "sans", "serif"] as const, defaults.font_family),
    locale: pick("locale", ["zh-CN", "en-US"] as const, defaults.locale),
    footer_text: typeof candidate.footer_text === "string" ? candidate.footer_text.replace(/[\u0000-\u001f\u007f]/g, " ").trim().slice(0, 120) : defaults.footer_text,
    pdf_page_size: pick("pdf_page_size", ["a4", "letter"] as const, defaults.pdf_page_size),
    pptx_aspect_ratio: pick("pptx_aspect_ratio", ["16:9", "4:3"] as const, defaults.pptx_aspect_ratio),
  };
}

export function loadPublicationTheme(defaults: PublicationTheme = defaultPublicationTheme): PublicationTheme {
  if (typeof window === "undefined") return defaults;
  try { return sanitizePublicationTheme(JSON.parse(localStorage.getItem(PUBLICATION_THEME_KEY) ?? "{}"), defaults); }
  catch { return defaults; }
}

export function savePublicationTheme(value: PublicationTheme) {
  localStorage.setItem(PUBLICATION_THEME_KEY, JSON.stringify(sanitizePublicationTheme(value)));
}

/** Dropdown options for a *_model field; null means "no catalog, use free text". */
export function modelOptionsFor(
  key: string,
  catalog: { backend?: string; models?: Array<{ name?: string }> } | null | undefined,
): string[] | null {
  if (!catalog || catalog.backend !== "litellm") return null;
  if (!key.endsWith("_model")) return null;
  const options = (catalog.models ?? []).map((entry) => entry.name).filter((name): name is string => Boolean(name));
  return options.length ? options : null;
}
