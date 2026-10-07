"use client";
import Link from "next/link";
import { researchFromKnowledgeHref } from "@/lib/knowledge-scope";
import type { KnowledgeSearchRequest } from "@/lib/contracts/knowledge";
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { LoadingSkeleton } from "@/components/ui/insight";
import { EmptyState, StatusBadge } from "@/components/ui/workspace";
import { knowledgeAnswer, type KnowledgeAnswerResponse } from "@/lib/knowledge-api";
import { KnowledgeEvidenceCard } from "./evidence-card";
import { useScopeFields } from "./scope-fields";

const statuses = { answered: "已生成答案", no_evidence: "缺少证据", rerank_unavailable: "重排暂不可用", citation_error: "引用校验未通过" };

export function AnswerPanel() {
  const { fields, buildRequest, ready } = useScopeFields();
  const [answer, setAnswer] = useState<KnowledgeAnswerResponse | null>(null);
  const [submittedRequest, setSubmittedRequest] = useState<KnowledgeSearchRequest>();
  const [selected, setSelected] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true); setError(""); setAnswer(null); setSelected([]);
    const request = buildRequest(); setSubmittedRequest(request);
    try { setAnswer(await knowledgeAnswer(request)); }
    catch (cause) { setError(String(cause)); }
    finally { setBusy(false); }
  };
  const citations = (answer?.citations ?? []).filter((citation) => citation.marker && citation.segment_ids.some((id) => answer?.evidence.some((item) => item.segment_id === id)));
  const markers = citations.map((citation) => citation.marker).sort((a, b) => b.length - a.length);
  const pattern = markers.length ? new RegExp(markers.map((marker) => marker.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"), "g") : null;
  const markdown = pattern ? (answer?.answer ?? "").replace(pattern, (marker) => `[${marker.replace(/[\[\]]/g, "\\$&")}](#answer-citation-${citations.findIndex((item) => item.marker === marker)})`) : answer?.answer ?? "";
  function selectCitation(index: number) {
    const ids = citations[index]?.segment_ids ?? [];
    setSelected(ids);
    const id = ids.find((candidate) => answer?.evidence.some((item) => item.segment_id === candidate));
    const target = document.getElementById(`knowledge-evidence-${id}`);
    target?.focus({ preventScroll: true });
    target?.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "center" });
  }
  return <section>
    <div className="document-toolbar">{fields}<button className="primary" disabled={!ready || busy} onClick={() => void run()}>{busy ? "生成中…" : "提问"}</button></div>
    {error && <div role="alert" className="document-operation error">{error}</div>}
    {busy && <LoadingSkeleton label="正在核对证据并生成答案…" />}
    {answer && <>
      <article className="knowledge-answer"><header><h3>基于资料的回答</h3><StatusBadge status={answer.status === "answered" ? "completed" : "partial"} label={statuses[answer.status]} /></header>
        {answer.message && <p role="status" className="empty-note">{answer.message}</p>}
        {answer.support && <p className="empty-note">支持度：{answer.support}</p>}
        <div className="knowledge-answer-copy"><ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: ({ href, children }) => {
          const match = href?.match(/^#answer-citation-(\d+)$/);
          return match && citations[Number(match[1])] ? <button type="button" className="knowledge-citation" aria-label={`查看引用 ${citations[Number(match[1])].marker}`} onClick={() => selectCitation(Number(match[1]))}>{children}</button> : <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>;
        } }}>{markdown}</ReactMarkdown></div>
        <div className="knowledge-result-actions">{submittedRequest && ((submittedRequest.kb_ids?.length ?? 0) + (submittedRequest.collection_ids?.length ?? 0) + (submittedRequest.document_ids?.length ?? 0) > 0) && <Link className="secondary" href={researchFromKnowledgeHref(submittedRequest)}>用这个问题继续研究</Link>}</div>
      </article>
      <h3>答案证据 · {answer.evidence.length}</h3>
      <div className="knowledge-results">{answer.evidence.map((item) => <KnowledgeEvidenceCard key={item.segment_id} item={item} selected={selected.includes(item.segment_id)} markers={citations.filter((citation) => citation.segment_ids.includes(item.segment_id)).map((citation) => citation.marker)} />)}</div>
      {!answer.evidence.length && <EmptyState title="暂无可用证据" description="调整问题或检索范围后重试。" />}
    </>}
  </section>;
}
