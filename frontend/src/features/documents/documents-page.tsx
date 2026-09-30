"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, FileText, LoaderCircle, RefreshCw, RotateCcw, Search, Trash2, Upload } from "lucide-react";
import Link from "next/link";
import { useRef, useState } from "react";
import { ChoiceGroup, EmptyState, PageHeading } from "@/components/ui/workspace";
import { AppShell } from "@/components/app-shell";
import { researchApi } from "@/lib/api";

const statusLabels: Record<string, string> = { queued: "等待摄取", processing: "解析 / OCR / 索引", ready: "可检索", failed: "摄取失败", deleting: "等待清理" };
const size = (bytes: number) => bytes >= 1024 ** 2 ? `${(bytes / 1024 ** 2).toFixed(1)} MiB` : `${Math.ceil(bytes / 1024)} KiB`;

export default function DocumentsPage() {
  const queryClient = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const [page, setPage] = useState(0);
  const pageSize = 50;
  const documents = useQuery({
    queryKey: ["documents", query, status, page], queryFn: () => researchApi.listDocuments({ q: query, status, limit: pageSize, offset: page * pageSize }), retry: false,
    refetchInterval: (state) => state.state.data?.items.some((item) => ["queued", "processing", "deleting"].includes(item.status)) ? 2_000 : false,
  });
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["documents"] });
  const upload = useMutation({ mutationFn: researchApi.uploadDocument, onSuccess: refresh });
  const retry = useMutation({ mutationFn: researchApi.retryDocument, onSuccess: refresh });
  const reindex = useMutation({ mutationFn: researchApi.reindexDocument, onSuccess: refresh });
  const remove = useMutation({ mutationFn: researchApi.deleteDocument, onSuccess: refresh });
  const addFiles = (files?: FileList | null) => files && Array.from(files).forEach((file) => upload.mutate(file));
  const inspector = <><h2 className="inspector-title">资料库状态</h2><div className="inspector-block"><div className="summary-grid"><div><b>{documents.data?.total ?? 0}</b><span>个人文档</span></div><div><b>{documents.data?.items.filter((item) => item.status === "ready").length ?? 0}</b><span>可检索</span></div></div></div><div className="inspector-block"><p className="eyebrow">证据边界</p><p className="empty-note">每次运行冻结为具体文档 ID。新上传文件不会自动加入已有研究，软删除也不会破坏历史引用。</p></div></>;
  return <AppShell inspector={inspector}><div className="page documents-page">
    <PageHeading title="让每份资料，都成为研究的依据" description="上传、处理与管理企业资料。处理完成后，即可选择用于研究。" actions={<><button className="primary" onClick={() => fileRef.current?.click()}><Upload size={15} /> 上传资料</button><input ref={fileRef} hidden multiple type="file" accept=".pdf,.docx,.xlsx,.pptx,.csv,.md,.txt,.png,.jpg,.jpeg,.tif,.tiff" onChange={(event) => addFiles(event.target.files)} /></>} />
    <div className="document-toolbar"><label><Search size={16} /><input aria-label="搜索文件名" value={query} onChange={(event) => { setQuery(event.target.value); setPage(0); }} placeholder="搜索资料名称…" /></label><button aria-label="刷新资料" onClick={() => void documents.refetch()}><RefreshCw size={16} /></button></div>
    <div className="library-filters"><ChoiceGroup label="资料状态" value={status} onChange={(value) => { setStatus(value); setPage(0); }} options={[{ value: "", label: "全部资料" }, ...Object.entries(statusLabels).map(([value, label]) => ({ value, label }))]} /></div>
    {documents.isPending && <EmptyState title="正在读取资料…" />}
    {upload.isPending && <div className="document-operation"><LoaderCircle className="spin" size={14} /> 正在流式上传并执行安全校验…</div>}
    {(upload.error || documents.error || retry.error || reindex.error || remove.error) && <div className="document-operation error">{(upload.error || documents.error || retry.error || reindex.error || remove.error)?.message}</div>}
    <div className="document-table" role="table" aria-label="个人资料库">
      <div className="document-table-head" role="row"><span>文件</span><span>状态</span><span>规模</span><span>更新时间</span><span>操作</span></div>
      {documents.data?.items.map((document) => <div className="document-table-row" role="row" key={document.id}>
        <Link className="document-name" href={`/documents/${document.id}`}><FileText size={17} /><span><b>{document.filename}</b><small>{document.media_type} · {document.sha256.slice(0, 10)}</small></span></Link>
        <span className="document-status" data-status={document.status}><i />{statusLabels[document.status]}</span>
        <span className="document-scale">{size(document.size_bytes)}<small>{document.page_count ?? 0} 页 / 单元 · {document.chunk_count} 个片段{document.ocr_pages ? ` · OCR ${document.ocr_pages}` : ""}</small></span>
        <time>{new Date(document.updated_at).toLocaleString("zh-CN")}</time>
        <span className="document-actions">{document.status === "failed" && <button title="重试摄取" onClick={() => retry.mutate(document.id)}><RotateCcw size={14} /></button>}{document.status === "ready" && <button title="重建索引" onClick={() => reindex.mutate(document.id)}><RefreshCw size={14} /></button>}<a title="下载原件" href={`/api/research/documents/${document.id}/content`}><Download size={14} /></a><button title="删除" onClick={() => remove.mutate(document.id)}><Trash2 size={14} /></button></span>
      </div>)}
      {!documents.isLoading && !documents.data?.items.length && <div className="document-empty"><FileText size={30} /><p>还没有匹配的资料</p><span>上传 PDF、Office、CSV、文本或图片后，摄取状态会显示在这里。</span></div>}
    </div>
    {documents.data && documents.data.total > pageSize && <div className="table-pagination"><button type="button" disabled={page === 0} onClick={() => setPage((value) => Math.max(0, value - 1))}>上一页</button><span>{page + 1} / {Math.ceil(documents.data.total / pageSize)}</span><button type="button" disabled={(page + 1) * pageSize >= documents.data.total} onClick={() => setPage((value) => value + 1)}>下一页</button></div>}
  </div></AppShell>;
}
