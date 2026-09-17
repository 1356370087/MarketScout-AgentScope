"use client";
import { useState } from "react";
import { knowledgeSearch, submitQueryFeedback, type KnowledgeSearchResponse } from "@/lib/knowledge-api";
import { useScopeFields } from "./scope-fields";

export function SearchPanel() {
  const { fields, buildRequest, ready } = useScopeFields();
  const [result, setResult] = useState<KnowledgeSearchResponse | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true); setError(""); setResult(null);
    try { setResult(await knowledgeSearch(buildRequest())); }
    catch (cause) { setError(String(cause)); }
    finally { setBusy(false); }
  };
  return (
    <section>
      <div className="document-toolbar">{fields}
        <button className="primary" disabled={!ready || busy} onClick={() => void run()}>{busy ? "检索中…" : "检索"}</button>
      </div>
      {error && <div className="document-operation error">{error}</div>}
      {result && (
        <>
          <div className="document-operation">
            命中 {result.results.length} 条 · 文档 {result.documents.length} 份
            {!result.rerank_completed && " · ⚠ 未完成重排（融合结果）"}
            <button onClick={() => void submitQueryFeedback(result.query_id, "not_found").catch(() => undefined)}>没找到？反馈</button>
          </div>
          <div className="knowledge-results">
            {result.results.map((item) => (
              <article key={item.segment_id} className="document-table-row">
                <b>{item.filename}</b>
                {item.relevance != null && <small> 相关性 {item.relevance}/3</small>}
                <p style={{ whiteSpace: "pre-wrap" }}>
                  {item.context_before && <small>{item.context_before.slice(-160)}…</small>}
                  {item.text}
                </p>
              </article>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

