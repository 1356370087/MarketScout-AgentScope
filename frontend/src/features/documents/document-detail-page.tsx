"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Download, MapPin } from "lucide-react";
import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { AppShell } from "@/components/app-shell";
import { LoadingSkeleton } from "@/components/ui/insight";
import { EmptyState, PageHeading, StatusBadge } from "@/components/ui/workspace";
import { isPublishedDocument } from "@/lib/source-selection";
import { researchApi } from "@/lib/api";

export default function DocumentDetailPage() {
  const { documentId } = useParams<{ documentId: string }>();
  const selectedChunk = useSearchParams().get("chunk");
  return <DocumentReader key={`${documentId}:${selectedChunk ?? "current"}`} documentId={documentId} selectedChunk={selectedChunk} />;
}

function DocumentReader({ documentId, selectedChunk }: { documentId: string; selectedChunk: string | null }) {
  const pageSize = 50;
  const [manualOffset, setOffset] = useState<number>();
  const metadata = useQuery({ queryKey: ["document", documentId], queryFn: () => researchApi.getDocument(documentId) });
  const target = useQuery({ queryKey: ["document-chunk", documentId, selectedChunk],
    queryFn: () => researchApi.documentChunk(documentId, selectedChunk!), enabled: !!selectedChunk, retry: false });
  const offset = manualOffset ?? (target.data ? Math.floor(target.data.ordinal / pageSize) * pageSize : 0);
  const generation = target.data?.generation_id ?? undefined;
  const chunks = useQuery({ queryKey: ["document-chunks", documentId, generation, offset],
    queryFn: () => researchApi.documentChunks(documentId, { offset, limit: pageSize, generation_id: generation }),
    enabled: selectedChunk ? target.isSuccess : !!metadata.data && isPublishedDocument(metadata.data) });
  const doc = metadata.data;
  const rows = chunks.data?.items ?? [];
  const showTarget = target.data && !rows.some((item) => item.id === target.data?.id) && manualOffset === undefined;
  useEffect(() => {
    if (!selectedChunk || !target.data || !chunks.data || manualOffset !== undefined) return;
    const element = document.getElementById(`chunk-${selectedChunk}`);
    element?.focus({ preventScroll: true });
    element?.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "center" });
  }, [selectedChunk, target.data, chunks.data, manualOffset]);
  const inspector = <><h2 className="inspector-title">资料信息</h2>{doc && <StatusBadge status={doc.status} />}<dl className="document-metadata">
    {doc && <><dt>文件类型</dt><dd>{doc.media_type}</dd><dt>大小</dt><dd>{(doc.size_bytes / 1024 / 1024).toFixed(1)} MiB</dd><dt>当前内容片段</dt><dd>{doc.chunk_count}</dd></>}
    {target.data && <><dt>引用版本</dt><dd>{target.data.version_no != null ? `版本 ${target.data.version_no}` : "已记录版本"}</dd><dt>引用代次</dt><dd>{target.data.generation_id}</dd><dt>资料位置</dt><dd>{target.data.locator}</dd></>}
  </dl>{(generation || doc?.current_generation_id) && <Link className="secondary" href={`/knowledge?review=1&document_id=${encodeURIComponent(documentId)}&generation_id=${encodeURIComponent(generation || doc!.current_generation_id!)}`}>核验所读代次</Link>}{doc?.failure_code && <p className="form-alert error">{doc.failure_code}</p>}</>;
  const display = showTarget ? [target.data!, ...rows] : rows;
  return <AppShell inspector={inspector}><div className="page document-detail-page">
    <Link className="ui-text-button" href="/documents"><ArrowLeft size={15} /> 资料库</Link>
    <PageHeading title={doc?.filename ?? (target.data ? "资料历史引用" : "读取文档…")} description={selectedChunk ? "正在阅读引用指向的具体片段与发布代次。" : "阅读已发布正文，核对原始内容与来源位置。"} actions={doc && <>{isPublishedDocument(doc) && <Link className="primary" href={`/research/new?document=${encodeURIComponent(doc.id)}`}>用于新研究</Link>}<a className="secondary" href={`/api/research/documents/${documentId}/content`}><Download size={15} /> 下载原件</a></>} />
    {target.data?.generation_status === "withdrawn" && <p className="form-alert" role="status">此来源版本已撤回，当前展示历史引用原文。</p>}
    {target.isError || chunks.isError || (metadata.isError && !target.data) ? <p role="alert" className="form-alert error">无法读取该资料或引用片段，请检查访问权限与发布状态。</p> : (selectedChunk ? target.isPending : metadata.isPending) || chunks.isLoading ? <LoadingSkeleton label="正在读取引用与正文…" /> : !selectedChunk && (!doc || !isPublishedDocument(doc)) ? <EmptyState title="暂无已发布正文" description="资料审核发布后，可在此阅读并用于研究。" /> : <>
      <section className="chunk-reader" aria-label="文档正文">{display.map((chunk) => <article id={`chunk-${chunk.id}`} tabIndex={-1} key={chunk.id} className={selectedChunk === chunk.id ? "targeted" : ""}><header><span><MapPin size={13} /> {chunk.locator}</span><span>片段 {chunk.ordinal}{chunk.version_no != null ? ` · 版本 ${chunk.version_no}` : ""}</span></header>{chunk.heading && <h2>{chunk.heading}</h2>}<p>{chunk.text}</p></article>)}{!display.length && <EmptyState title="暂无已发布正文片段" description="资料需要先完成审核发布，才能作为研究依据。" />}</section>
      <div className="table-pagination"><button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - pageSize))}>上一页</button><span>第 {Math.floor(offset / pageSize) + 1} 页</span><button type="button" disabled={rows.length < pageSize} onClick={() => setOffset(offset + pageSize)}>下一页</button></div>
    </>}
  </div></AppShell>;
}
