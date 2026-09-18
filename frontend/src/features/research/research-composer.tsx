"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
import { useRouter } from "next/navigation";
import { useMemo, useRef, useState, type FormEvent } from "react";
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
      <i data-status={document.status}>{document.status}</i>
    </label>)}
    {!documents.length && <p className="empty-note">资料库中还没有可选文件。</p>}
  </div>;
}

export function ResearchComposer() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<SourceMode>("web");
  const [selected, setSelected] = useState<string[]>([]);
  const [urls, setUrls] = useState("");
  const [domains, setDomains] = useState("");
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
  const upload = useMutation({
    mutationFn: researchApi.uploadDocument,
    onSuccess: async (result) => {
      await queryClient.invalidateQueries({ queryKey: ["documents"] });
      if (result.document.status === "ready") setSelected((current) => [...new Set([...current, result.document.id])]);
    },
  });
  const refs = buildSourceRefs(mode, selected, urls, domains);
  const validSources = sourceSelectionIsValid(mode, refs);
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
  const addFiles = (files: FileList | File[]) => Array.from(files).forEach((file) => upload.mutate(file));

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
    <div className="composer-mode-options" style={{ display: "flex", gap: 16, padding: 16, flexWrap: "wrap" }}>
      <label>研究执行模式 <select aria-label="研究执行模式" value={selectedResearchMode} onChange={(event) => setResearchMode(event.target.value)}>
        <option value="sync">同步研究</option><option value="collaborator">异步 · Collaborator（Lead 委派）</option><option value="teams">异步 · Agent Teams（团队协作）</option>
      </select></label>
      {selectedResearchMode === "teams" && <label>团队默认执行方式 <select aria-label="团队默认执行方式" value={selectedTeamMode} onChange={(event) => setTeamMode(event.target.value)}>
        <option value="direct">直接执行</option><option value="plan_approval">先规划，由 Lead 审批</option>
      </select><small> Lead 创建团队，启动成员时可单独调整。</small></label>}
    </div>
    {!query && <div className="composer-template-row" aria-label="竞品分析模板">
      {promptTemplates.map(({ label, icon: Icon, prompt }) => <button key={label} type="button" onClick={() => setQuery(prompt)}><Icon size={14} /><span>{label}</span></button>)}
    </div>}
    <textarea
      aria-label="研究问题"
      value={query}
      onChange={(event) => setQuery(event.target.value)}
      placeholder={"描述公司、竞品、时间范围与希望支持的决策……\n例如：对比三家企业 AI 助手的产品能力、定价与渠道策略。"}
    />
    <div className="source-mode-bar" role="tablist" aria-label="研究来源模式">
      {modes.map(({ value, label, icon: Icon }) => <button
        key={value}
        type="button"
        role="tab"
        aria-selected={mode === value}
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
        <span>{upload.isPending ? "正在上传并校验…" : "拖入文件，或从企业资料库选择"}</span>
        <button type="button" title="上传文件" onClick={() => fileRef.current?.click()}><Paperclip size={15} /></button>
        <input ref={fileRef} hidden type="file" multiple accept=".pdf,.docx,.xlsx,.pptx,.csv,.md,.txt,.png,.jpg,.jpeg,.tif,.tiff" onChange={(event) => event.target.files && addFiles(event.target.files)} />
      </div>
      {upload.isError && <p className="source-error">{upload.error.message}</p>}
      {documents.isLoading ? <p className="empty-note"><LoaderCircle className="spin" size={13} /> 正在读取资料库…</p> : <DocumentPicker documents={documents.data?.items ?? []} selected={selected} onToggle={(id) => setSelected((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id])} />}
      <div className="source-config-footer"><span>{selected.length} 个文件已选择</span><button type="button" onClick={() => void documents.refetch()} title="刷新状态"><RefreshCw size={13} /></button>{selected.length > 0 && <button type="button" onClick={() => setSelected([])} title="清空已选文件"><X size={13} /></button>}</div>
    </div>}
    {(createError || (!validSources && mode !== "web")) && <p className="composer-error" role="alert">{createError || "请选择符合当前模式的研究来源。"}</p>}
    <div className="composer-footer">
      <div className="composer-context">
        <span className="source-ready"><i />{sourceLabel}</span>
        <Link href="/settings" title="打开研究配置"><SlidersHorizontal size={14} /><span>{String(activeSettings.report_type ?? "default")} · {activeSettings.enable_human_in_loop ? "人工复核" : "自动执行"}</span></Link>
      </div>
      <Button className="composer-send" type="submit" disabled={creating || !query.trim() || !validSources} aria-label="启动研究">
        <span>{creating ? "正在创建" : "启动研究"}</span><ArrowUp size={17} />
      </Button>
    </div>
  </form>;
}
