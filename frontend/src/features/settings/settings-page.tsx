"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useQuery } from "@tanstack/react-query";
import { Download, RotateCcw, Save, Upload } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useForm, useWatch } from "react-hook-form";
import { z } from "zod";
import { ChoiceGroup, EmptyState, PageHeading, SearchSelect } from "@/components/ui/workspace";
import { Disclosure, LoadingSkeleton, MetricStrip } from "@/components/ui/insight";
import "./settings-ui.css";
import { AppShell } from "@/components/app-shell";
import { researchApi } from "@/lib/api";
import { applyPublicationThemePreset, defaultPublicationTheme, loadPublicationTheme, loadSettings, modelChoicesFor, modelOptionsFor, sanitizePublicationTheme, sanitizeSettings, savePublicationTheme, saveSettings, settingFieldBounds, settingFieldType, settingGroups } from "@/lib/settings";
import type { PublicationTheme } from "@/lib/types";

type FieldSchema = { type?: string; title?: string; description?: string; default?: unknown; minimum?: number; maximum?: number; enum?: unknown[]; anyOf?: Array<{ type?: string; enum?: unknown[] }> };
type Capabilities = { editable_config_keys: string[]; defaults: Record<string, unknown>; config_schema: { properties: Record<string, FieldSchema>; $defs?: Record<string, FieldSchema> }; features?: { memory?: boolean }; publication_theme_defaults?: PublicationTheme };
type ModelCatalog = { backend?: string; models?: Array<{ name?: string }>; stale?: boolean; error?: string | null };
const fieldLabels: Record<string, string> = {"allow_clarification": "开始前澄清问题", "enable_async_research": "启用异步研究", "async_research_mode": "异步协作方式", "team_execution_mode": "团队执行方式", "summarization_model": "摘要模型", "summarization_model_max_tokens": "摘要输出上限", "research_model": "研究模型", "research_model_max_tokens": "研究输出上限", "compression_model": "压缩模型", "compression_model_max_tokens": "压缩输出上限", "final_report_model": "报告模型", "final_report_model_max_tokens": "报告输出上限", "search_api": "搜索服务", "web_pipeline_mode": "网页处理模式", "web_min_source_authority": "来源权威性要求", "search_candidate_limit": "候选来源数量", "max_fetches_per_researcher": "每个研究员的抓取上限", "max_concurrent_research_units": "并行研究任务上限", "max_researcher_iterations": "研究迭代上限", "max_react_tool_calls": "工具调用上限", "enable_human_in_loop": "启用人工确认", "hitl_require_plan_approval": "研究计划需要确认", "hitl_require_outline_approval": "报告大纲需要确认", "hitl_max_plan_revisions": "计划修订上限", "hitl_feedback_mode": "反馈方式", "report_type": "报告类型", "output_format": "交付格式", "quality_evaluation_enabled": "启用质量评估", "quality_evaluation_model": "质量评估模型", "quality_evaluation_rigor": "评估严格程度", "quality_evaluation_min_sources": "最少来源数", "quality_evaluation_max_input_chars": "评估输入字符上限", "report_review_enabled": "启用报告复核", "report_review_model": "报告复核模型", "report_review_max_revisions": "报告修订上限", "enable_memory": "启用研究记忆", "memory_top_k": "召回记忆数量", "memory_auto_write": "自动保存记忆"};
const optionLabels: Record<string, string> = { direct: "直接执行", plan_approval: "先规划再审批", collaborator: "异步委派", teams: "团队协作", none: "不使用", default: "标准报告", strict: "严格", balanced: "均衡", conservative: "保守", enforced: "强制执行", shadow: "影子模式", legacy: "兼容模式", manual: "手动", auto: "自动" };
const advancedKeys = new Set(["summarization_model_max_tokens", "research_model_max_tokens", "compression_model_max_tokens", "final_report_model_max_tokens", "web_pipeline_mode", "web_min_source_authority", "search_candidate_limit", "max_fetches_per_researcher", "max_researcher_iterations", "max_react_tool_calls", "hitl_max_plan_revisions", "quality_evaluation_max_input_chars", "report_review_max_revisions", "memory_top_k"]);
const settingsSchema = z.record(z.string(), z.unknown());

function SettingControl({ name, schema, value, onChange, options }: { name: string; schema: FieldSchema; value: unknown; onChange: (value: unknown) => void; options?: string[] | null }) {
  const enums = schema.enum ?? schema.anyOf?.flatMap((item) => item.enum ?? []) ?? [];
  const type = settingFieldType(schema, value);
  if (type === "boolean") return <button id={name} type="button" role="switch" aria-label={fieldLabels[name] ?? name} aria-checked={Boolean(value)} className={`switch ${value ? "on" : ""}`} onClick={() => onChange(!value)}><i /></button>;
  if (enums.length) {
    const choices = enums.map((item) => ({ value: String(item), label: optionLabels[String(item)] ?? String(item) }));
    return choices.length <= 4 ? <ChoiceGroup label={fieldLabels[name] ?? schema.title ?? name} value={String(value ?? "")} options={choices} onChange={(next) => onChange(next || null)} /> : <SearchSelect id={name} label={fieldLabels[name] ?? schema.title ?? name} value={String(value ?? "")} options={choices} onChange={(next) => onChange(next || null)} />;
  }
  if (options && options.length) {
    const current = String(value ?? "");
    const choices = modelChoicesFor(schema, value, options);
    const nullable = schema.anyOf?.some((item) => item.type === "null") ?? false;
    return <SearchSelect id={name} label={fieldLabels[name] ?? schema.title ?? name} value={current} onChange={(next) => onChange(next || null)} options={choices.map((item) => ({ value: item, label: item || (nullable ? "跟随质量评估模型" : "未指定") }))} />;
  }
  if (type === "number" || type === "integer") {
    const { minimum: min, maximum: max } = settingFieldBounds(schema, value);
    return <><input aria-label={`${name} slider`} type="range" min={min} max={max} step={type === "integer" ? 1 : .01} value={Number(value ?? min)} onChange={(event) => onChange(Number(event.target.value))} /><input id={name} type="number" min={min} max={max} value={Number(value ?? min)} onChange={(event) => onChange(Number(event.target.value))} /></>;
  }
  return <input id={name} type="text" value={String(value ?? "")} onChange={(event) => onChange(event.target.value)} />;
}

export default function SettingsPage() {
  const [activeGroup, setActiveGroup] = useState("basic");
  const [notice, setNotice] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const [publicationTheme, setPublicationTheme] = useState<PublicationTheme>(defaultPublicationTheme);
  const { data, error } = useQuery({ queryKey: ["capabilities"], queryFn: () => researchApi.capabilities() as Promise<Capabilities> });
  const { data: modelCatalog } = useQuery({ queryKey: ["model-catalog"], queryFn: () => researchApi.models() as Promise<ModelCatalog>, staleTime: 60_000 });
  const { control, setValue, reset, handleSubmit } = useForm<Record<string, unknown>>({ resolver: zodResolver(settingsSchema), defaultValues: {} });
  const values = useWatch({ control }) as Record<string, unknown>;
  useEffect(() => {
    if (!data) return;
    reset(loadSettings(data.defaults));
    const nextTheme = loadPublicationTheme(data.publication_theme_defaults ?? defaultPublicationTheme);
    let active = true;
    queueMicrotask(() => {
      if (active) setPublicationTheme(nextTheme);
    });
    return () => {
      active = false;
    };
  }, [data, reset]);
  const save = handleSubmit((form) => { saveSettings(sanitizeSettings(form, data ?? {})); savePublicationTheme(publicationTheme); setNotice("设置已保存，将用于新建研究。"); });
  function exportJson() { const blob = new Blob([JSON.stringify({ configurable: values, publication_theme: publicationTheme }, null, 2)], { type: "application/json" }); const url = URL.createObjectURL(blob); const a = document.createElement("a"); a.href = url; a.download = "odr-settings.json"; a.click(); URL.revokeObjectURL(url); }
  async function importJson(file?: File) { if (!file || !data) return; const parsed = JSON.parse(await file.text()); const configurable = parsed && typeof parsed === "object" && "configurable" in parsed ? parsed.configurable : parsed; reset({ ...data.defaults, ...sanitizeSettings(configurable, data) }); if (parsed && typeof parsed === "object" && "publication_theme" in parsed) setPublicationTheme(sanitizePublicationTheme(parsed.publication_theme, data.publication_theme_defaults ?? defaultPublicationTheme)); }
  function renderField(key: string) {
    const schema = data?.config_schema.properties[key] ?? {};
    return <div className="setting-row" key={key}><header><label htmlFor={key}>{fieldLabels[key] ?? schema.title ?? key.replaceAll("_", " ")}</label></header><SettingControl name={key} schema={schema} value={values[key] ?? data?.defaults[key]} onChange={(value) => setValue(key, value, { shouldDirty: true, shouldValidate: true })} options={modelOptionsFor(key, modelCatalog)} />{schema.description && <small>{schema.description}</small>}<details className="setting-field-id"><summary>配置字段</summary><code>{key}</code></details></div>;
  }
  return <AppShell><div className="page settings-page"><PageHeading title="研究设置" description="让研究按你的方式进行。已开始的研究保留创建时配置。" />{notice && <p role="status" className="form-alert">{notice}</p>}{error ? <p role="alert" className="form-alert error">无法读取可用设置，请稍后刷新。</p> : !data ? <LoadingSkeleton label="正在读取可用设置…" /> : <form noValidate onSubmit={save} className="settings-layout"><nav className="settings-nav" aria-label="设置分类">{settingGroups.filter((group) => group.id !== "memory" || data.features?.memory).map((group) => <button key={group.id} type="button" aria-pressed={activeGroup === group.id} onClick={() => setActiveGroup(group.id)}>{group.label}</button>)}<button type="button" aria-pressed={activeGroup === "publication"} onClick={() => setActiveGroup("publication")}>发布主题</button></nav><div className="settings-sections"><MetricStrip label="新研究配置摘要" items={[{ label: "研究模型", value: String(values.research_model ?? data.defaults.research_model ?? "服务端默认") }, { label: "执行方式", value: values.enable_async_research ? values.async_research_mode === "teams" ? "团队协作" : "异步委派" : "同步研究" }, { label: "人工确认", value: values.enable_human_in_loop ? "已启用" : "自动执行" }]} />{settingGroups.filter((group) => group.id !== "memory" || data.features?.memory).map((group) => <section className="settings-group" key={group.id} hidden={activeGroup !== group.id}><header><h2>{group.label}</h2><p>这些偏好仅适用于新建研究。</p></header><div className="settings-fields">{!group.keys.some((key) => data.editable_config_keys.includes(key)) && <EmptyState title="当前服务未开放此类设置" description="可修改的偏好由服务端配置决定。" />}{group.keys.filter((key) => data.editable_config_keys.includes(key) && !advancedKeys.has(key)).map(renderField)}{group.keys.some((key) => data.editable_config_keys.includes(key) && advancedKeys.has(key)) && <Disclosure title="高级参数" meta="输出预算、上限与执行策略"><div className="settings-fields">{group.keys.filter((key) => data.editable_config_keys.includes(key) && advancedKeys.has(key)).map(renderField)}</div></Disclosure>}</div></section>)}<section className="settings-group" hidden={activeGroup !== "publication"}><header><h2>发布主题</h2><p>设置报告文档的外观与交付格式。</p></header><div className="settings-fields publication-theme-fields"><label className="setting-row"><header><span>主题预设</span><code>preset</code></header><select value={publicationTheme.preset} onChange={(event) => setPublicationTheme(applyPublicationThemePreset(publicationTheme, event.target.value as PublicationTheme["preset"]))}><option value="default">Default</option><option value="boardroom">Boardroom</option><option value="academic">Academic</option></select></label><label className="setting-row"><header><span>主色</span><code>primary_color</code></header><input type="color" value={publicationTheme.primary_color} onChange={(event) => setPublicationTheme({ ...publicationTheme, primary_color: event.target.value.toUpperCase() })} /></label><label className="setting-row"><header><span>强调色</span><code>accent_color</code></header><input type="color" value={publicationTheme.accent_color} onChange={(event) => setPublicationTheme({ ...publicationTheme, accent_color: event.target.value.toUpperCase() })} /></label><label className="setting-row"><header><span>字体族</span><code>font_family</code></header><select value={publicationTheme.font_family} onChange={(event) => setPublicationTheme({ ...publicationTheme, font_family: event.target.value as PublicationTheme["font_family"] })}><option value="cjk_sans">CJK Sans</option><option value="sans">Sans</option><option value="serif">Serif</option></select></label><label className="setting-row"><header><span>标签语言</span><code>locale</code></header><select value={publicationTheme.locale} onChange={(event) => setPublicationTheme({ ...publicationTheme, locale: event.target.value as PublicationTheme["locale"] })}><option value="zh-CN">中文</option><option value="en-US">English</option></select></label><label className="setting-row"><header><span>页面尺寸</span><code>pdf_page_size</code></header><select value={publicationTheme.pdf_page_size} onChange={(event) => setPublicationTheme({ ...publicationTheme, pdf_page_size: event.target.value as PublicationTheme["pdf_page_size"] })}><option value="a4">A4</option><option value="letter">Letter</option></select></label><label className="setting-row"><header><span>幻灯片比例</span><code>pptx_aspect_ratio</code></header><select value={publicationTheme.pptx_aspect_ratio} onChange={(event) => setPublicationTheme({ ...publicationTheme, pptx_aspect_ratio: event.target.value as PublicationTheme["pptx_aspect_ratio"] })}><option value="16:9">16:9</option><option value="4:3">4:3</option></select></label><label className="setting-row"><header><span>页脚</span><code>footer_text</code></header><input value={publicationTheme.footer_text} maxLength={120} onChange={(event) => setPublicationTheme({ ...publicationTheme, footer_text: event.target.value })} /></label></div></section><div className="settings-actions"><button className="primary" type="submit"><Save size={15} /> 保存设置</button><button className="secondary" type="button" onClick={() => { reset(data.defaults); setPublicationTheme(data.publication_theme_defaults ?? defaultPublicationTheme); }}><RotateCcw size={15} /> 恢复默认</button><button className="secondary" type="button" onClick={exportJson}><Download size={15} /> 导出 JSON</button><button className="secondary" type="button" onClick={() => fileRef.current?.click()}><Upload size={15} /> 导入 JSON</button><input ref={fileRef} hidden type="file" accept="application/json" onChange={(event) => void importJson(event.target.files?.[0]).catch(() => setNotice("导入失败，请检查配置文件格式。"))} /></div></div></form>}</div></AppShell>;
}
