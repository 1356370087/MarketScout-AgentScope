"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useQuery } from "@tanstack/react-query";
import { Download, RotateCcw, Save, Upload } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useForm, useWatch } from "react-hook-form";
import { z } from "zod";
import { AppShell } from "@/components/app-shell";
import { researchApi } from "@/lib/api";
import { applyPublicationThemePreset, defaultPublicationTheme, loadPublicationTheme, loadSettings, modelChoicesFor, modelOptionsFor, sanitizePublicationTheme, sanitizeSettings, savePublicationTheme, saveSettings, settingFieldBounds, settingFieldType, settingGroups } from "@/lib/settings";
import type { PublicationTheme } from "@/lib/types";

type FieldSchema = { type?: string; title?: string; description?: string; default?: unknown; minimum?: number; maximum?: number; enum?: unknown[]; anyOf?: Array<{ type?: string; enum?: unknown[] }> };
type Capabilities = { editable_config_keys: string[]; defaults: Record<string, unknown>; config_schema: { properties: Record<string, FieldSchema>; $defs?: Record<string, FieldSchema> }; features?: { memory?: boolean }; publication_theme_defaults?: PublicationTheme };
type ModelCatalog = { backend?: string; models?: Array<{ name?: string }>; stale?: boolean; error?: string | null };
const settingsSchema = z.record(z.string(), z.unknown());

function SettingControl({ name, schema, value, onChange, options }: { name: string; schema: FieldSchema; value: unknown; onChange: (value: unknown) => void; options?: string[] | null }) {
  const enums = schema.enum ?? schema.anyOf?.flatMap((item) => item.enum ?? []) ?? [];
  const type = settingFieldType(schema, value);
  if (type === "boolean") return <button id={name} type="button" role="switch" aria-label={name} aria-checked={Boolean(value)} className={`switch ${value ? "on" : ""}`} onClick={() => onChange(!value)}><i /></button>;
  if (enums.length) return <select id={name} aria-label={name} value={String(value ?? "")} onChange={(event) => onChange(event.target.value || null)}>{enums.map((item) => <option key={String(item)} value={String(item)}>{String(item)}</option>)}</select>;
  if (options && options.length) {
    const current = String(value ?? "");
    const choices = modelChoicesFor(schema, value, options);
    const nullable = schema.anyOf?.some((item) => item.type === "null") ?? false;
    return <select id={name} aria-label={name} value={current} onChange={(event) => onChange(event.target.value || null)}>{choices.map((item) => <option key={item || "__unset"} value={item}>{item || (nullable ? "跟随质量评估模型" : "未指定")}</option>)}</select>;
  }
  if (type === "number" || type === "integer") {
    const { minimum: min, maximum: max } = settingFieldBounds(schema, value);
    return <><input aria-label={`${name} slider`} type="range" min={min} max={max} step={type === "integer" ? 1 : .01} value={Number(value ?? min)} onChange={(event) => onChange(Number(event.target.value))} /><input id={name} type="number" min={min} max={max} value={Number(value ?? min)} onChange={(event) => onChange(Number(event.target.value))} /></>;
  }
  return <input id={name} type="text" value={String(value ?? "")} onChange={(event) => onChange(event.target.value)} />;
}

export default function SettingsPage() {
  const fileRef = useRef<HTMLInputElement>(null);
  const [publicationTheme, setPublicationTheme] = useState<PublicationTheme>(defaultPublicationTheme);
  const { data } = useQuery({ queryKey: ["capabilities"], queryFn: () => researchApi.capabilities() as Promise<Capabilities> });
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
  const save = handleSubmit((form) => { saveSettings(sanitizeSettings(form, data ?? {})); savePublicationTheme(publicationTheme); });
  function exportJson() { const blob = new Blob([JSON.stringify({ configurable: values, publication_theme: publicationTheme }, null, 2)], { type: "application/json" }); const url = URL.createObjectURL(blob); const a = document.createElement("a"); a.href = url; a.download = "odr-settings.json"; a.click(); URL.revokeObjectURL(url); }
  async function importJson(file?: File) { if (!file || !data) return; const parsed = JSON.parse(await file.text()); const configurable = parsed && typeof parsed === "object" && "configurable" in parsed ? parsed.configurable : parsed; reset({ ...data.defaults, ...sanitizeSettings(configurable, data) }); if (parsed && typeof parsed === "object" && "publication_theme" in parsed) setPublicationTheme(sanitizePublicationTheme(parsed.publication_theme, data.publication_theme_defaults ?? defaultPublicationTheme)); }
  const inspector = <><h2 className="inspector-title">配置安全边界</h2><div className="inspector-block"><p className="empty-note">仅展示 capabilities 明确白名单中的用户字段。导入时未知字段会被剔除，最终仍由服务端再次校验。</p></div><div className="inspector-block"><p className="eyebrow">永不落盘到浏览器</p><p className="empty-note">JWT、模型 API Key、MCP 配置、端点凭据、沙箱和域名白名单。</p></div></>;
  return <AppShell inspector={inspector}><div className="page"><header className="page-header"><div><span className="eyebrow">USER CONFIG / VERSION 1</span><h1>配置研究行为，<br />不越过安全边界。</h1><p>所有设置保存在版本化本地预设中。运行创建时发送，服务端白名单是最终权威。</p></div></header>{!data ? <div className="panel panel-body">正在读取服务端 capabilities…</div> : <form noValidate onSubmit={save} className="settings-grid">{settingGroups.filter((group) => group.id !== "memory" || data.features?.memory).map((group, index) => <details className="settings-group" key={group.id} open={index < 2}><summary><strong>{group.label}</strong><span className="mono">{group.keys.filter((key) => data.editable_config_keys.includes(key)).length} FIELDS</span></summary><div className="settings-fields">{group.keys.filter((key) => data.editable_config_keys.includes(key)).map((key) => { const schema = data.config_schema.properties[key] ?? {}; return <div className="setting-row" key={key}><header><label htmlFor={key}>{schema.title ?? key.replaceAll("_", " ")}</label><code>{key}</code></header><SettingControl name={key} schema={schema} value={values[key] ?? data.defaults[key]} onChange={(value) => setValue(key, value, { shouldDirty: true, shouldValidate: true })} options={modelOptionsFor(key, modelCatalog)} />{schema.description && <small>{schema.description}</small>}</div>; })}</div></details>)}<details className="settings-group"><summary><strong>发布主题</strong><span className="mono">08 FIELDS</span></summary><div className="settings-fields publication-theme-fields"><label className="setting-row"><header><span>主题预设</span><code>preset</code></header><select value={publicationTheme.preset} onChange={(event) => setPublicationTheme(applyPublicationThemePreset(publicationTheme, event.target.value as PublicationTheme["preset"]))}><option value="default">Default</option><option value="boardroom">Boardroom</option><option value="academic">Academic</option></select></label><label className="setting-row"><header><span>主色</span><code>primary_color</code></header><input type="color" value={publicationTheme.primary_color} onChange={(event) => setPublicationTheme({ ...publicationTheme, primary_color: event.target.value.toUpperCase() })} /></label><label className="setting-row"><header><span>强调色</span><code>accent_color</code></header><input type="color" value={publicationTheme.accent_color} onChange={(event) => setPublicationTheme({ ...publicationTheme, accent_color: event.target.value.toUpperCase() })} /></label><label className="setting-row"><header><span>字体族</span><code>font_family</code></header><select value={publicationTheme.font_family} onChange={(event) => setPublicationTheme({ ...publicationTheme, font_family: event.target.value as PublicationTheme["font_family"] })}><option value="cjk_sans">CJK Sans</option><option value="sans">Sans</option><option value="serif">Serif</option></select></label><label className="setting-row"><header><span>标签语言</span><code>locale</code></header><select value={publicationTheme.locale} onChange={(event) => setPublicationTheme({ ...publicationTheme, locale: event.target.value as PublicationTheme["locale"] })}><option value="zh-CN">中文</option><option value="en-US">English</option></select></label><label className="setting-row"><header><span>页面尺寸</span><code>pdf_page_size</code></header><select value={publicationTheme.pdf_page_size} onChange={(event) => setPublicationTheme({ ...publicationTheme, pdf_page_size: event.target.value as PublicationTheme["pdf_page_size"] })}><option value="a4">A4</option><option value="letter">Letter</option></select></label><label className="setting-row"><header><span>幻灯片比例</span><code>pptx_aspect_ratio</code></header><select value={publicationTheme.pptx_aspect_ratio} onChange={(event) => setPublicationTheme({ ...publicationTheme, pptx_aspect_ratio: event.target.value as PublicationTheme["pptx_aspect_ratio"] })}><option value="16:9">16:9</option><option value="4:3">4:3</option></select></label><label className="setting-row"><header><span>页脚</span><code>footer_text</code></header><input value={publicationTheme.footer_text} maxLength={120} onChange={(event) => setPublicationTheme({ ...publicationTheme, footer_text: event.target.value })} /></label></div></details><div className="settings-actions"><button className="primary" type="submit"><Save size={15} /> 保存设置</button><button className="secondary" type="button" onClick={() => { reset(data.defaults); setPublicationTheme(data.publication_theme_defaults ?? defaultPublicationTheme); }}><RotateCcw size={15} /> 恢复默认</button><button className="secondary" type="button" onClick={exportJson}><Download size={15} /> 导出 JSON</button><button className="secondary" type="button" onClick={() => fileRef.current?.click()}><Upload size={15} /> 导入 JSON</button><input ref={fileRef} hidden type="file" accept="application/json" onChange={(event) => void importJson(event.target.files?.[0])} /></div></form>}</div></AppShell>;
}
