"use client";

import { ArrowUpRight, Bot, ExternalLink, FileText, Globe2, Link2, X } from "lucide-react";
import { EmptyState } from "@/components/ui/workspace";
import { Disclosure, SearchField } from "@/components/ui/insight";
import { useEffect, useMemo, useRef, useState } from "react";
import type { ResearchSource, ResearchTask } from "@/lib/types";
import { sourceDomain, sourceHref, sourceKey } from "../activity/presentation";

export function SourceCard({ source, selected, onSelect, taskTitle }: {
  source: ResearchSource; selected?: boolean; onSelect?: (source: ResearchSource) => void; taskTitle?: string;
}) {
  const href = sourceHref(source.url);
  const local = source.source_type === "local_document" || source.url.startsWith("/documents/");
  const title = source.title || sourceDomain(source);
  return <article className={`evidence-card ${selected ? "selected" : ""}`}>
    <div className="evidence-card-heading"><span className="evidence-site" aria-hidden>{local ? <FileText size={17} /> : <Globe2 size={17} />}</span>
      <div>{onSelect ? <button type="button" className="evidence-title" aria-pressed={selected} onClick={() => onSelect(source)}>{title}</button> : <strong>{title}</strong>}<span className="evidence-domain">{sourceDomain(source)}</span></div>
      {href && <a className="evidence-open" aria-label={`打开来源：${title}`} href={href} target={local ? undefined : "_blank"} rel="noopener noreferrer"><ExternalLink size={14} /></a>}
    </div>
    <div className="evidence-card-meta"><span>{local ? "企业资料" : "网页来源"}</span>{taskTitle && <span><Bot size={12} aria-hidden />{taskTitle}</span>}</div>
  </article>;
}

export function SourceDetail({ source, task, onTask, onFindCitation, onClose }: {
  source: ResearchSource; task?: ResearchTask; onTask?: (taskId: string) => void; onFindCitation?: () => void; onClose?: () => void;
}) {
  const href = sourceHref(source.url);
  const detail = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!window.matchMedia("(max-width: 1199px)").matches) detail.current?.focus({ preventScroll: true });
  }, [source.source_id, source.url]);
  return <section ref={detail} tabIndex={-1} className="source-detail" aria-label="来源详情">
    <header><h2>来源详情</h2>{onClose && <button className="ui-icon" type="button" aria-label="返回来源列表" onClick={onClose}><X size={16} /></button>}</header>
    <SourceCard source={source} />
    <dl className="source-facts"><div><dt>所属任务</dt><dd>{task?.title || source.task_id || "尚未记录归属"}</dd></div>{source.chunk_id && <div><dt>资料位置</dt><dd>{source.locator || "已关联文档片段"}</dd></div>}{source.generation_id && <div><dt>发布代次</dt><dd>{source.generation_id}</dd></div>}</dl>
    <div className="source-relations">
      {task && source.task_id && onTask && <button className="secondary" type="button" onClick={() => onTask(source.task_id!)}><Bot size={15} />查看关联任务<ArrowUpRight size={14} /></button>}
      {onFindCitation && <button className="secondary" type="button" onClick={onFindCitation}><Link2 size={15} />在报告中查找引用</button>}
      {href && <a className="ui-text-button" href={href} target={href.startsWith("/") ? undefined : "_blank"} rel="noopener noreferrer">{href.startsWith("/") ? "阅读资料原文" : "访问来源网页"}<ExternalLink size={13} /></a>}
    </div>
    <Disclosure title="来源地址"><p className="source-address">{source.url}</p></Disclosure>
  </section>;
}

export function EvidenceExplorer({ sources, tasks, selectedSource, onSelect, compact = false, taskId }: {
  sources: ResearchSource[]; tasks: ResearchTask[]; selectedSource?: string | null; onSelect: (source: ResearchSource) => void; compact?: boolean; taskId?: string | null;
}) {
  const [query, setQuery] = useState("");
  const [type, setType] = useState("all");
  const [taskFilter, setTaskFilter] = useState("");
  const [domain, setDomain] = useState("");
  const scope = taskId ?? taskFilter;
  const domains = useMemo(() => [...new Set(sources.map(sourceDomain))].sort(), [sources]);
  const filtered = useMemo(() => sources.filter((source) => {
    const local = source.source_type === "local_document" || source.url.startsWith("/documents/");
    return (!scope || source.task_id === scope) && (type === "all" || (type === "local") === local)
      && (!domain || sourceDomain(source) === domain)
      && `${source.title ?? ""} ${sourceDomain(source)} ${source.url}`.toLocaleLowerCase().includes(query.toLocaleLowerCase());
  }), [sources, scope, type, domain, query]);
  return <section className={`evidence-explorer ${compact ? "compact" : ""}`} aria-label="来源证据库">
    <div className="evidence-toolbar"><SearchField label="搜索来源" value={query} onChange={setQuery} placeholder="标题、域名或地址…" />
      {!compact && <div className="evidence-filters"><label>类型<select aria-label="来源类型" value={type} onChange={(event) => setType(event.target.value)}><option value="all">全部类型</option><option value="web">网页</option><option value="local">企业资料</option></select></label>
        {!taskId && <label>任务<select aria-label="来源所属任务" value={taskFilter} onChange={(event) => setTaskFilter(event.target.value)}><option value="">全部任务</option>{tasks.map((task) => <option key={task.task_id} value={task.task_id}>{task.title || task.task_id}</option>)}</select></label>}
        <label>域名<select aria-label="来源域名" value={domain} onChange={(event) => setDomain(event.target.value)}><option value="">全部域名</option>{domains.map((item) => <option key={item}>{item}</option>)}</select></label></div>}
    </div>
    <p className="evidence-count" role="status">{filtered.length} 个来源{scope && ` · ${tasks.find((task) => task.task_id === scope)?.title ?? scope}`}</p>
    <div className="evidence-grid">{filtered.map((source) => <SourceCard key={sourceKey(source)} source={source} selected={selectedSource === sourceKey(source)} onSelect={onSelect} taskTitle={tasks.find((task) => task.task_id === source.task_id)?.title} />)}</div>
    {!filtered.length && <EmptyState title={sources.length ? "没有匹配的来源" : "正在等待来源"} description={sources.length ? "调整筛选条件查看已记录来源。" : "已公开的网页与资料来源会随研究持续更新。"} />}
  </section>;
}
