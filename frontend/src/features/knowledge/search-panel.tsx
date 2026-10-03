"use client";
import Link from "next/link";
import { useState } from "react";
import { LoadingSkeleton, MetricStrip } from "@/components/ui/insight";
import { EmptyState } from "@/components/ui/workspace";
import { knowledgeSearch, submitQueryFeedback, type KnowledgeSearchResponse } from "@/lib/knowledge-api";
import { KnowledgeEvidenceCard } from "./evidence-card";
import { useScopeFields } from "./scope-fields";

export function SearchPanel() {
  const { fields, buildRequest, ready } = useScopeFields();
  const [result, setResult] = useState<KnowledgeSearchResponse | null>(null);
  const [submittedQuery, setSubmittedQuery] = useState("");
  const [error, setError] = useState("");
  const [feedback, setFeedback] = useState("");
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true); setError(""); setResult(null); setFeedback("");
    const request = buildRequest(); setSubmittedQuery(request.query);
    try { setResult(await knowledgeSearch(request)); }
    catch (cause) { setError(String(cause)); }
    finally { setBusy(false); }
  };
  return <section>
    <div className="document-toolbar">{fields}<button className="primary" disabled={!ready || busy} onClick={() => void run()}>{busy ? "检索中…" : "检索"}</button></div>
    {error && <div role="alert" className="document-operation error">{error}</div>}
    {busy && <LoadingSkeleton label="正在检索相关证据…" />}
    {result && <>
      <div className="knowledge-result-summary"><MetricStrip label="检索结果" items={[{ label: "命中片段", value: result.results.length }, { label: "相关资料", value: result.documents.length }, { label: "排序方式", value: result.rerank_completed ? "已完成重排" : "融合结果，未重排" }]} /></div>
      <div className="knowledge-result-actions"><Link className="secondary" href={`/research/new?${new URLSearchParams({ query: submittedQuery })}`}>用这个问题继续研究</Link><button type="button" className="ui-text-button" disabled={!!feedback} onClick={() => { setFeedback("正在提交…"); void submitQueryFeedback(result.query_id, "not_found").then(() => setFeedback("反馈已记录")).catch((cause) => { setError(String(cause)); setFeedback(""); }); }}>没找到？反馈</button>{feedback && <span role="status">{feedback}</span>}</div>
      <div className="knowledge-results">{result.results.map((item) => <KnowledgeEvidenceCard key={item.segment_id} item={item} />)}</div>
      {!result.results.length && <EmptyState title="没有找到相关证据" description="尝试调整问题或检索范围。" />}
    </>}
  </section>;
}
