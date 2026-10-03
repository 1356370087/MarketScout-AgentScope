"use client";

import { useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, FileText, LoaderCircle, XCircle } from "lucide-react";
import { useState } from "react";
import { Disclosure } from "@/components/ui/insight";
import { researchApi } from "@/lib/api";
import type { ResearchDocument } from "@/lib/types";
import "./documents-ui.css";

type UploadItem = { id: string; filename: string; state: "uploading" | "accepted" | "failed"; message: string };

export function useDocumentUploads(onUploaded?: (document: ResearchDocument) => void) {
  const client = useQueryClient();
  const [queue, setQueue] = useState<UploadItem[]>([]);
  function addFiles(files: FileList | File[]) {
    for (const file of Array.from(files)) {
      const id = crypto.randomUUID();
      setQueue((current) => [...current, { id, filename: file.name, state: "uploading", message: "上传与安全校验中" }]);
      void researchApi.uploadDocument(file).then((result) => {
        setQueue((current) => current.map((item) => item.id === id ? { ...item, state: "accepted", message: result.document.status === "ready" ? "已可检索" : "已上传，处理状态见资料库" } : item));
        onUploaded?.(result.document);
        void client.invalidateQueries({ queryKey: ["documents"] });
      }).catch((error: unknown) => {
        setQueue((current) => current.map((item) => item.id === id ? { ...item, state: "failed", message: error instanceof Error ? error.message : "上传失败，请重新选择文件" } : item));
      });
    }
  }
  return { queue, addFiles };
}

export function UploadQueue({ items }: { items: UploadItem[] }) {
  if (!items.length) return null;
  const pending = items.filter((item) => item.state === "uploading").length;
  return <Disclosure title="上传队列" meta={pending ? `${pending} 份上传中` : `${items.length} 份已处理`} open>
    <ul className="document-upload-queue" aria-live="polite">{items.map((item) => <li key={item.id} data-state={item.state}><FileText size={17} aria-hidden /><div><b>{item.filename}</b><span>{item.message}</span></div>{item.state === "uploading" ? <LoaderCircle className="spin" size={16} aria-hidden /> : item.state === "accepted" ? <CheckCircle2 size={16} aria-hidden /> : <XCircle size={16} aria-hidden />}</li>)}</ul>
  </Disclosure>;
}
