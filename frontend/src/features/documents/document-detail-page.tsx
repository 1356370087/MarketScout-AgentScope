"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Download, FileText, MapPin } from "lucide-react";
import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { useEffect } from "react";
import { AppShell } from "@/components/app-shell";
import { researchApi } from "@/lib/api";

export default function DocumentDetailPage() {
  const { documentId } = useParams<{ documentId: string }>();
  const selectedChunk = useSearchParams().get("chunk");
  const documentQuery = useQuery({ queryKey: ["document", documentId], queryFn: () => researchApi.getDocument(documentId) });
  const chunks = useQuery({ queryKey: ["document-chunks", documentId], queryFn: () => researchApi.documentChunks(documentId), enabled: documentQuery.data?.status === "ready" });
  useEffect(() => { if (selectedChunk && chunks.data) document.getElementById(`chunk-${selectedChunk}`)?.scrollIntoView({ behavior: "smooth", block: "center" }); }, [selectedChunk, chunks.data]);
  return <AppShell><div className="page document-detail-page">
    <header className="document-detail-header"><Link href="/documents"><ArrowLeft size={15} /> 资料库</Link><div><span className="eyebrow">EXTRACTED SOURCE / OWNER ONLY</span><h1>{documentQuery.data?.filename ?? "读取文档…"}</h1><p>{documentQuery.data ? `${documentQuery.data.chunk_count} chunks · OCR ${documentQuery.data.ocr_pages} units · ${documentQuery.data.status}` : ""}</p></div>{documentQuery.data && <a className="secondary" href={`/api/research/documents/${documentId}/content`}><Download size={14} /> 下载原件</a>}</header>
    <main className="chunk-reader">{chunks.data?.items.map((chunk) => <article id={`chunk-${chunk.id}`} key={chunk.id} className={selectedChunk === chunk.id ? "targeted" : ""}><header><span><MapPin size={12} /> {chunk.locator}</span><code>CHUNK {String(chunk.ordinal).padStart(3, "0")}</code></header>{chunk.heading && <h2>{chunk.heading}</h2>}<p>{chunk.text}</p></article>)}{documentQuery.data?.status !== "ready" && <div className="document-empty"><FileText size={30} /><p>文档尚未完成索引</p><span>摄取完成后可在这里检查结构定位和抽取正文。</span></div>}</main>
  </div></AppShell>;
}
