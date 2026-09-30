"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Download, MapPin } from "lucide-react";
import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { useEffect } from "react";
import { AppShell } from "@/components/app-shell";
import { EmptyState, PageHeading, StatusBadge } from "@/components/ui/workspace";
import { researchApi } from "@/lib/api";

export default function DocumentDetailPage() {
  const { documentId } = useParams<{ documentId: string }>();
  const selectedChunk = useSearchParams().get("chunk");
  const documentQuery = useQuery({ queryKey: ["document", documentId], queryFn: () => researchApi.getDocument(documentId) });
  const chunks = useQuery({ queryKey: ["document-chunks", documentId], queryFn: () => researchApi.documentChunks(documentId), enabled: documentQuery.data?.status === "ready" });
  const doc = documentQuery.data;
  useEffect(() => { if (selectedChunk && chunks.data) document.getElementById(`chunk-${selectedChunk}`)?.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "center" }); }, [selectedChunk, chunks.data]);
  const inspector = doc && <><h2 className="inspector-title">资料信息</h2><StatusBadge status={doc.status} /><dl className="document-metadata"><dt>文件类型</dt><dd>{doc.media_type}</dd><dt>大小</dt><dd>{(doc.size_bytes / 1024 / 1024).toFixed(1)} MiB</dd><dt>内容片段</dt><dd>{doc.chunk_count}</dd><dt>更新时间</dt><dd>{new Date(doc.updated_at).toLocaleString("zh-CN")}</dd>{doc.current_generation_id && <><dt>当前解析代次</dt><dd>{doc.current_generation_id}</dd></>}</dl>{doc.failure_code && <p className="form-alert error">{doc.failure_code}</p>}{doc.current_generation_id && <Link className="secondary" href={`/knowledge?review=1&document_id=${encodeURIComponent(documentId)}&generation_id=${encodeURIComponent(doc.current_generation_id)}`}>核验当前代次</Link>}<p className="empty-note">文件处理完成与知识库发布是不同状态。核验与发布记录可在审核工作台查看。</p></>;
  return <AppShell inspector={inspector}><div className="page document-detail-page"><Link className="ui-text-button" href="/documents"><ArrowLeft size={15} /> 资料库</Link><PageHeading title={doc?.filename ?? "读取文档…"} description="阅读已提取正文，核对原始内容与来源位置。" actions={doc && <a className="secondary" href={`/api/research/documents/${documentId}/content`}><Download size={15} /> 下载原件</a>} />
    {documentQuery.isError || chunks.isError ? <p role="alert" className="form-alert error">无法读取资料，请稍后刷新或检查访问权限。</p> : documentQuery.isPending || chunks.isFetching ? <EmptyState title="正在读取文档内容…" /> : doc?.status !== "ready" ? <EmptyState title="文档尚未完成处理" description="处理完成后可在这里检查来源位置与正文。" /> : <section className="chunk-reader" aria-label="文档正文">{chunks.data?.items.map((chunk) => <article id={`chunk-${chunk.id}`} key={chunk.id} className={selectedChunk === chunk.id ? "targeted" : ""}><header><span><MapPin size={13} /> {chunk.locator}</span><span>片段 {chunk.ordinal}</span></header>{chunk.heading && <h2>{chunk.heading}</h2>}<p>{chunk.text}</p></article>)}{!chunks.data?.items.length && <EmptyState title="暂无可显示的正文片段" />}</section>}
  </div></AppShell>;
}
