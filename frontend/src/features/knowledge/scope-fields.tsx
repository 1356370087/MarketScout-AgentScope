"use client";
import { useState, useEffect } from "react";
import { SearchSelect, SurfaceDialog } from "@/components/ui/workspace";
import { docTypes } from "./document-types";

export function useScopeFields() {
  const [query, setQuery] = useState("");
  const [kbIds, setKbIds] = useState("");
  const [documentIds, setDocumentIds] = useState("");
  const [docType, setDocType] = useState("");
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    setQuery(params.get("query") ?? "");
    setKbIds(params.get("kb_id") ?? "");
  }, []);
  const buildRequest = () => ({
    query,
    kb_ids: kbIds.split(/[\s,]+/).filter(Boolean),
    document_ids: documentIds.split(/[\s,]+/).filter(Boolean),
    filters: docType ? { doc_types: [docType] } : {},
  });
  const fields = (
    <>
      <label>
        <input value={query} onChange={(event) => setQuery(event.target.value)} aria-label="检索 / 提问内容" placeholder="检索 / 提问内容" />
      </label>
      <SurfaceDialog title="检索范围" description="可选：限定知识库、资料与类型。留空时按现有权限检索。" trigger={<button type="button">范围与筛选</button>}><div className="ui-form-fields">
      <label>知识库范围
        <input aria-label="知识库范围" value={kbIds} onChange={(event) => setKbIds(event.target.value)} placeholder="知识库 ID（可选，逗号分隔）" />
      </label>
      <label>文档范围
        <input aria-label="文档范围" value={documentIds} onChange={(event) => setDocumentIds(event.target.value)} placeholder="文档 ID（可选，逗号分隔）" />
      </label>
      <SearchSelect label="资料类型" value={docType} onChange={setDocType} options={[{ value: "", label: "全部资料类型" }, ...docTypes.map(type => ({ value: type, label: type }))]} />
      </div></SurfaceDialog>
    </>
  );
  return { query, fields, buildRequest, ready: query.trim().length > 0 };
}

