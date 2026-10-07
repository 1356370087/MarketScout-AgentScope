"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Disclosure, LoadingSkeleton, SearchField } from "@/components/ui/insight";
import { ChoiceGroup, EmptyState, SearchSelect } from "@/components/ui/workspace";
import { apiFetch } from "@/lib/api/http";
import type { MaterialScope } from "@/lib/knowledge-scope";
import { docTypes } from "./document-types";
import "./knowledge-ui.css";

type Base = { id: string; name: string; description?: string; workspace_id?: string };
export function useKnowledgeBases(enabled = true) {
  return useQuery({ queryKey: ["knowledge-bases", "readable"], enabled,
    queryFn: () => apiFetch<{ items: Base[] }>("/knowledge-bases?archived=false") });
}

function Collections({ base, value, onChange }: { base: Base; value: MaterialScope; onChange: (value: MaterialScope) => void }) {
  const [open, setOpen] = useState(false);
  const data = useQuery({ queryKey: ["knowledge-collections", base.id], enabled: open,
    queryFn: () => apiFetch<{ items: { id: string; name: string }[] }>(`/knowledge-bases/${encodeURIComponent(base.id)}/collections`) });
  return <div className="material-collections"><button type="button" className="ui-text-button" aria-expanded={open} onClick={() => setOpen(!open)}>选择 {base.name} 的集合</button>{open && <>
    {data.isPending && <LoadingSkeleton label="正在读取集合…" rows={1} />}{data.isError && <p role="alert">集合读取失败。</p>}
    {data.data?.items.map((item) => <label key={item.id}><input type="checkbox" checked={value.collection_ids.includes(item.id)} onChange={() => onChange({ ...value, kb_ids: value.kb_ids.filter((id) => id !== base.id), collection_ids: value.collection_ids.includes(item.id) ? value.collection_ids.filter((id) => id !== item.id) : [...value.collection_ids, item.id] })} /><span>{item.name}</span></label>)}
    {data.isSuccess && !data.data.items.length && <p className="empty-note">此知识库尚无集合。</p>}
  </>}</div>;
}

export function MaterialScopePicker({ value, onChange, allowAll = false }: { value: MaterialScope; onChange: (value: MaterialScope) => void; allowAll?: boolean }) {
  const bases = useKnowledgeBases();
  const profiles = useQuery({ queryKey: ["knowledge-profiles"], queryFn: () => apiFetch<{ index_profile?: { model: string; dimensions: number; revision: string }; items: { version: string; note?: string; is_default: boolean }[] }>("/knowledge/search-profiles") });
  const [search, setSearch] = useState("");
  return <div className="material-scope-picker">
    <SearchField label="搜索知识库" value={search} onChange={setSearch} />
    {allowAll && !value.kb_ids.length && !value.collection_ids.length && !value.document_ids.length && <p className="empty-note">当前范围为全部可访问知识库。</p>}
    {bases.isPending ? <LoadingSkeleton label="正在读取可访问知识库…" /> : bases.isError ? <p role="alert">知识库读取失败，请刷新重试。</p> : !bases.data.items.length ? <EmptyState title="暂无可访问知识库" description="可选择已有文档，或先上传并发布资料。" /> : <div className="material-base-list">{bases.data.items.filter((base) => `${base.name} ${base.description ?? ""}`.toLocaleLowerCase().includes(search.toLocaleLowerCase())).map((base) => <article key={base.id}><label><input type="checkbox" checked={value.kb_ids.includes(base.id)} onChange={() => onChange({ ...value, kb_ids: value.kb_ids.includes(base.id) ? value.kb_ids.filter((id) => id !== base.id) : [...value.kb_ids, base.id] })} /><span><b>{base.name}</b><small>{base.description || "选择全部已发布资料"}</small></span></label><Collections base={base} value={value} onChange={onChange} /></article>)}</div>}
    {(value.kb_ids.length > 0 || value.collection_ids.length > 0) && <button className="ui-text-button" type="button" onClick={() => onChange({ ...value, kb_ids: [], collection_ids: [] })}>清空库与集合选择</button>}
    <p className="empty-note">已选 {value.kb_ids.length} 个库、{value.collection_ids.length} 个集合、{value.document_ids.length} 份指定文档。研究创建时固定实际资料代次。</p>
    <Disclosure title="版本与检索设置" meta={value.version_mode === "as_of" ? "历史资料" : "当前发布资料"}>
      <div className="ui-form-fields">{profiles.data?.index_profile && <p className="empty-note">索引模型 {profiles.data.index_profile.model} · {profiles.data.index_profile.dimensions} 维 · {profiles.data.index_profile.revision}</p>}<ChoiceGroup label="资料版本" value={value.version_mode ?? "current"} onChange={(mode) => onChange({ ...value, version_mode: mode as MaterialScope["version_mode"], as_of_published: mode === "current" ? null : value.as_of_published })} options={[{ value: "current", label: "当前已发布" }, { value: "as_of", label: "历史发布时点" }]} />
        {value.version_mode === "as_of" && <label>发布时间截止<input type="date" aria-label="发布时间截止" value={value.as_of_published ?? ""} onChange={(event) => onChange({ ...value, as_of_published: event.target.value || null })} /><small>使用该日期 00:00 UTC 前已发布的资料。</small></label>}
        <label>业务有效日期<input type="date" aria-label="业务有效日期" value={value.as_of_valid ?? ""} onChange={(event) => onChange({ ...value, as_of_valid: event.target.value || null })} /></label>
        <SearchSelect label="资料类型" value={value.filters?.doc_types?.[0] ?? ""} onChange={(type) => onChange({ ...value, filters: { ...value.filters, doc_types: type ? [type] : [] } })} options={[{ value: "", label: "全部资料类型" }, ...docTypes.map((type) => ({ value: type, label: type }))]} />
        <SearchSelect label="检索配置" value={value.profile_version ?? ""} onChange={(profile) => onChange({ ...value, profile_version: profile || null })} options={[{ value: "", label: "服务默认配置" }, ...(profiles.data?.items ?? []).map((profile) => ({ value: profile.version, label: profile.note || profile.version }))]} />
        {profiles.isError && <p role="status" className="empty-note">暂时无法读取配置目录，创建时由服务端核对所选配置。</p>}
      </div>
    </Disclosure>
  </div>;
}
