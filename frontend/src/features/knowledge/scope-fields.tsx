"use client";
import { useState, useEffect } from "react";
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
        <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="检索 / 提问内容" style={{ minWidth: 260 }} />
      </label>
      <label>
        <input value={kbIds} onChange={(event) => setKbIds(event.target.value)} placeholder="知识库 ID（可选，逗号分隔）" />
      </label>
      <label>
        <input value={documentIds} onChange={(event) => setDocumentIds(event.target.value)} placeholder="文档 ID（可选，逗号分隔）" />
      </label>
      <label>
        <select value={docType} onChange={(event) => setDocType(event.target.value)}>
          <option value="">全部资料类型</option>
          {docTypes.map((type) => (
            <option key={type} value={type}>{type}</option>
          ))}
        </select>
      </label>
    </>
  );
  return { query, fields, buildRequest, ready: query.trim().length > 0 };
}

