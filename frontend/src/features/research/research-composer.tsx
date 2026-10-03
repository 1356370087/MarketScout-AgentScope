"use client";

import { useQuery } from "@tanstack/react-query";
import {
  ArrowUp,
  FileText,
  GitCompareArrows,
  Globe2,
  Link2,
  LoaderCircle,
  Paperclip,
  RefreshCw,
  SearchCheck,
  ShieldCheck,
  SlidersHorizontal,
  Telescope,
  UploadCloud,
  X,
} from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useMemo, useRef, useState, type FormEvent } from "react";
import { ChoiceGroup, SurfaceDialog, StatusBadge } from "@/components/ui/workspace";
import { UploadQueue, useDocumentUploads } from "@/features/documents/document-upload";
import { Button } from "@/components/ui/button";
import { researchApi } from "@/lib/api";
import { loadPublicationTheme, loadSettings } from "@/lib/settings";
import { buildSourceRefs, sourceSelectionIsValid } from "@/lib/source-selection";
import type { ResearchDocument, SourceMode, SourceSelection } from "@/lib/types";

const modes: Array<{ value: SourceMode; label: string; icon: typeof Globe2 }> = [
  { value: "web", label: "公开网络", icon: Globe2 },
  { value: "documents", label: "企业资料", icon: FileText },
  { value: "hybrid", label: "混合证据", icon: SearchCheck },
  { value: "specific", label: "指定来源", icon: Link2 },
];

const promptTemplates = [
  {
    label: "横向对标",
    icon: GitCompareArrows,
    prompt: "对比两家核心竞品在目标客户、产品能力、定价、渠道和增长策略上的差异，输出可验证的竞争矩阵与未来 12 个月风险。",
  },
  {
    label: "护城河拆解",
    icon: ShieldCheck,
    prompt: "分析目标公司的市场位置、增长引擎、商业模式、核心护城河与主要替代方案，并明确区分事实、推断和证据缺口。",
  },
  {
    label: "战略信号追踪",
    icon: Telescope,
    prompt: "跟踪竞品最近两个季度的产品发布、招聘、定价和合作伙伴信号，判断其下一步战略方向及对我们的潜在影响。",
  },
];

function DocumentPicker({ documents, selected, onToggle }: { documents: ResearchDocument[]; selected: string[]; onToggle: (id: string) => void }) {
  return <div className="composer-doc-list">
    {documents.map((document) => <label key={document.id} className={`composer-doc-row ${selected.includes(document.id) ? "selected" : ""}`}>
      <input type="checkbox" checked={selected.includes(document.id)} disabled={document.status !== "ready"} onChange={() => onToggle(document.id)} />
      <FileText size={15} />
      <span><b>{document.filename}</b><small>{document.chunk_count} 个片段 · {(document.size_bytes / 1024 / 1024).toFixed(1)} MiB</small></span>
      <StatusBadge status={document.status} />
    </label>)}
    {!documents.length && <p className="empty-note">资料库中还没有可选文件。</p>}
  </div>;
}

export function ResearchComposer() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const fileRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState(searchParams.get("query") ?? "");
  const [mode, setMode] = useState<SourceMode>(searchParams.has("document") ? "documents" : "web");
  const [selected, setSelected] = useState<string[]>(() => [...new Set(searchParams.getAll("document").filter(Boolean))]);
  const [urls, setUrls] = useState("");
  const [domains, setDomains] = useState("");
  const [pickerOpen, setPickerOpen] = useState(false);
  const [docSearch, setDocSearch] = useState("");
  const [dragging, setDragging] = useState(false);
  const [creating, setCreating] = useState(false);
  const [researchMode, setResearchMode] = useState<string>();
  const [teamMode, setTeamMode] = useState<string>();
  const [createError, setCreateError] = useState("");
  const capabilities = useQuery({ queryKey: ["capabilities"], queryFn: researchApi.capabilities, retry: false });
  const documentCapability = (capabilities.data?.features as { document_research?: { enabled?: boolean; database?: string } } | undefined)?.document_research;
  const docsEnabled = Boolean(documentCapability?.enabled && documentCapability.database === "ready");
  const documents = useQuery({
    queryKey: ["documents", "composer"],
    queryFn: () => researchApi.listAllDocuments(),
    enabled: docsEnabled,
    retry: false,
    refetchInterval: (state) => state.state.data?.items.some((item) => ["queued", "processing"].includes(item.status)) ? 2_000 : false,
  });
  const upload = useDocumentUploads((document) => {
    if (document.status === "ready") setSelected((current) => [...new Set([...current, document.id])]);
  });
  const selectedDocumentsReady = !selected.length || (docsEnabled && documents.isSuccess && selected.every((id) => documents.data.items.some((document) => document.id === id && document.status === "ready")));
  const refs = buildSourceRefs(mode, selected, urls, domains);
  const validSources = sourceSelectionIsValid(mode, refs) && selectedDocumentsReady && (mode !== "documents" && mode !== "hybrid" || docsEnabled);
  const selection = useMemo<SourceSelection>(() => ({ mode, sources: refs }), [mode, refs]);
  const activeSettings = loadSettings(capabilities.data?.defaults);
  const selectedResearchMode = researchMode ?? (activeSettings.enable_async_research ? String(activeSettings.async_research_mode ?? "collaborator") : "sync");
  const selectedTeamMode = teamMode ?? String(activeSettings.team_execution_mode ?? "direct");
  const sourceLabel = modes.find((item) => item.value === mode)?.label ?? mode;

  const chooseMode = (value: SourceMode) => {
    if (["documents", "hybrid"].includes(value) && !docsEnabled) return;
    setMode(value);
    if (value === "web") setSelected([]);
  };
  const addFiles = (files: FileList | File[]) => { if (docsEnabled) upload.addFiles(files); if (fileRef.current) fileRef.current.value = ""; };

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const researchQuery = query.trim();
    if (creating || !researchQuery || !validSources) return;
    setCreating(true);
    setCreateError("");
    try {
      const created = await researchApi.createRun(
        researchQuery,
        { ...loadSettings(capabilities.data?.defaults), enable_async_research: selectedResearchMode !== "sync",
          async_research_mode: selectedResearchMode === "teams" ? "teams" : "collaborator", team_execution_mode: selectedTeamMode },
        researchQuery.slice(0, 80),
        selection,
        loadPublicationTheme(),
      );
      router.push(`/research/${created.run_id}`);
    } catch (cause) {
      setCreateError(cause instanceof Error ? cause.message : "研究任务创建失败，请稍后重试。");
    } finally {
      setCreating(false);
    }
  }

  return <form className="research-composer research-composer-expanded" onSubmit={submit}>
    <div className="composer-preferences"><SurfaceDialog title="研究偏好" description="本次研究使用的执行方式。" trigger={<button type="button" className="ui-text-button"><SlidersHorizontal size={15} /> 研究偏好</button>}>
      <ChoiceGroup label="研究执行模式" value={selectedResearchMode} onChange={setResearchMode} options={[{ value: "sync", label: "同步研究", description: "按研究计划执行" }, { value: "collaborator", label: "异步委派", description: "由 Lead 分配研究任务" }, { value: "teams", label: "团队协作", description: "多个成员协同研究" }]} />
      {selectedResearchMode === "teams" && <ChoiceGroup label="团队默认执行方式" value={selectedTeamMode} onChange={setTeamMode} options={[{ value: "direct", label: "直接执行" }, { value: "plan_approval", label: "先规划，由 Lead 审批" }]} />}
      <p className="empty-note">Lead 创建团队，启动成员时可单独调整。</p>
    </SurfaceDialog></div>
    {!query && <div className="composer-template-row" aria-label="竞品分析模板">
      {promptTemplates.map(({ label, icon: Icon, prompt }) => <button key={label} type="button" onClick={() => setQuery(prompt)}><Icon size={16} /><span>{label}<small>{prompt.split("，")[0]}</small></span></button>)}
    </div>}
    <textarea
      aria-label="研究问题"
      value={query}
      onChange={(event) => setQuery(event.target.value)}
      placeholder={"描述公司、竞品、时间范围与希望支持的决策……\n例如：对比三家企业 AI 助手的产品能力、定价与渠道策略。"}
    />
    <div className="source-mode-bar" role="group" aria-label="研究来源模式">
      {modes.map(({ value, label, icon: Icon }) => <button
        key={value}
        type="button"
        aria-pressed={mode === value}
        disabled={!docsEnabled && ["documents", "hybrid"].includes(value)}
        className={mode === value ? "active" : ""}
        onClick={() => chooseMode(value)}
      ><Icon size={14} />{label}</button>)}
    </div>
    {mode !== "web" && <div className="source-config">
      {mode === "specific" && <div className="specific-source-grid">
        <label><span>精确网址</span><textarea value={urls} onChange={(event) => setUrls(event.target.value)} placeholder="https://example.com/report" /></label>
        <label><span>限定域名</span><textarea value={domains} onChange={(event) => setDomains(event.target.value)} placeholder="example.com" /></label>
      </div>}
      <div
        className={`composer-dropzone ${dragging ? "dragging" : ""}`}
        onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => { event.preventDefault(); setDragging(false); addFiles(event.dataTransfer.files); }}
      >
        <UploadCloud size={17} />
        <span>{docsEnabled ? "拖入文件，或从企业资料库选择" : "当前服务尚未开放资料上传"}</span>
        <button type="button" title="上传文件" disabled={!docsEnabled} onClick={() => fileRef.current?.click()}><Paperclip size={15} /></button>
        <input ref={fileRef} hidden type="file" multiple accept=".pdf,.docx,.xlsx,.pptx,.csv,.md,.txt,.png,.jpg,.jpeg,.tif,.tiff" onChange={(event) => event.target.files && addFiles(event.target.files)} />
      </div>
      <UploadQueue items={upload.queue} />
      <SurfaceDialog title="选择研究资料" description="只有已完成处理的资料可以加入研究。" open={pickerOpen} onOpenChange={setPickerOpen} trigger={<button type="button" className="secondary composer-picker-trigger" disabled={!docsEnabled}><FileText size={15} /> 从资料库选择 · 已选 {selected.length} 份</button>}>
        <label className="ui-search"><input aria-label="搜索研究资料" placeholder="搜索文件名…" value={docSearch} onChange={(event) => setDocSearch(event.target.value)} /></label>
        {documents.isLoading ? <p className="empty-note"><LoaderCircle className="spin" size={13} /> 正在读取资料库…</p> : documents.isError ? <p role="alert">资料库读取失败，请刷新重试。</p> : <DocumentPicker documents={(documents.data?.items ?? []).filter((document) => document.filename.toLocaleLowerCase().includes(docSearch.toLocaleLowerCase()))} selected={selected} onToggle={(id) => setSelected((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id])} />}
        <div className="ui-actions"><button type="button" className="primary" onClick={() => setPickerOpen(false)}>完成选择</button></div>
      </SurfaceDialog>
      <div className="composer-selected-docs" aria-label="已选研究资料">{selected.map((id) => <span key={id}><FileText size={13} /><b>{documents.data?.items.find((document) => document.id === id)?.filename ?? (documents.isLoading ? "读取资料中…" : "资料不可用")}</b><button type="button" aria-label={`移除 ${documents.data?.items.find((document) => document.id === id)?.filename ?? id}`} onClick={() => setSelected((current) => current.filter((item) => item !== id))}><X size={13} /></button></span>)}</div>
      {!selectedDocumentsReady && <p className="source-error" role="status">正在核对已选资料，尚未就绪或不可访问的资料需移除后继续。</p>}
      <div className="source-config-footer"><span>{selected.length} 个文件已选择</span><button type="button" onClick={() => void documents.refetch()} title="刷新状态"><RefreshCw size={13} /></button>{selected.length > 0 && <button type="button" onClick={() => setSelected([])} title="清空已选文件"><X size={13} /></button>}</div>
    </div>}
    {(createError || (!validSources && mode !== "web")) && <p className="composer-error" role="alert">{createError || "请选择符合当前模式的研究来源。"}</p>}
    <dl className="composer-config-summary" aria-label="本次研究配置"><div><dt>研究方式</dt><dd>{{ sync: "同步研究", collaborator: "异步委派", teams: "团队协作" }[selectedResearchMode]}</dd></div><div><dt>研究模型</dt><dd>{String(activeSettings.research_model ?? "服务端默认")}</dd></div><div><dt>资料范围</dt><dd>{sourceLabel}{selected.length ? ` · ${selected.length} 份资料` : ""}</dd></div></dl>
    <div className="composer-footer">
      <div className="composer-context">
        <span className="source-ready"><i />{sourceLabel}</span>
        <Link href="/settings" title="打开研究配置"><SlidersHorizontal size={14} /><span>{activeSettings.report_type && activeSettings.report_type !== "default" ? String(activeSettings.report_type) : "标准报告"} · {activeSettings.enable_human_in_loop ? "人工复核" : "自动执行"}</span></Link>
      </div>
      <Button className="composer-send" type="submit" disabled={creating || !query.trim() || !validSources} aria-label="启动研究">
        <span>{creating ? "正在创建" : "启动研究"}</span><ArrowUp size={17} />
      </Button>
    </div>
  </form>;
}
