"use client";
import { useState } from "react";
import { knowledgeAnswer, type KnowledgeAnswerResponse } from "@/lib/knowledge-api";
import { useScopeFields } from "./scope-fields";

export function AnswerPanel() {
  const { fields, buildRequest, ready } = useScopeFields();
  const [answer, setAnswer] = useState<KnowledgeAnswerResponse | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true); setError(""); setAnswer(null);
    try { setAnswer(await knowledgeAnswer(buildRequest())); }
    catch (cause) { setError(String(cause)); }
    finally { setBusy(false); }
  };
  return (
    <section>
      <div className="document-toolbar">{fields}
        <button className="primary" disabled={!ready || busy} onClick={() => void run()}>{busy ? "生成中…" : "提问"}</button>
      </div>
      {error && <div className="document-operation error">{error}</div>}
      {answer && (
        <>
          <div className="document-operation">
            状态：{answer.status}
            {answer.support && ` · 支持度 ${answer.support}`}
            {answer.message && ` · ${answer.message}`}
          </div>
          {answer.answer && <article className="document-table-row"><p style={{ whiteSpace: "pre-wrap" }}>{answer.answer}</p></article>}
          <h3>证据</h3>
          <div className="knowledge-results">
            {answer.evidence.map((item) => (
              <article key={item.segment_id} className="document-table-row">
                <b>{item.filename}</b>
                <p style={{ whiteSpace: "pre-wrap" }}>{item.text}</p>
              </article>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

