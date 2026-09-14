"use client";

import { useState, useEffect } from "react";
import Link from "next/link";
import { AppShell } from "@/components/app-shell";
import {
  confirmMetadata,
  fetchGenerationReview,
  knowledgeAnswer,
  knowledgeSearch,
  publishGeneration,
  reviseUnit,
  submitQueryFeedback,
  type GenerationReview,
  type KnowledgeAnswerResponse,
  type KnowledgeSearchResponse,
} from "@/lib/knowledge-api";

const docTypes = ["财报", "价格表", "产品文档", "行业报告", "新闻", "内部材料", "其他"];

export default function KnowledgePage() {
  const [tab, setTab] = useState<"search" | "answer" | "review">("search");
  const inspector = (
    <>
      <h2 className="inspector-title">知识库</h2>
      <div className="inspector-block">
        <p className="eyebrow">检索边界</p>
        <p className="empty-note">仅检索已发布代次；范围、版本与权限过滤发生在数据库内。重排失败会明确标注，不会伪装成“没有答案”。</p>
      </div>
    </>
  );
  return (
    <AppShell inspector={inspector}>
      <div className="page knowledge-page">
        <header className="page-header">
          <div>
            <span className="eyebrow">KNOWLEDGE BASE / RETRIEVAL &amp; QA</span>
            <h1>统一检索与单轮资料问答。</h1>
            <p>三路召回 + 语义重排 + 同文档配额；回答只依据本次证据并逐段引用。</p>
          </div>
        </header>
        <div className="document-toolbar">
          <Link href="/knowledge/ledger">事实台账、Wiki 与资料归档</Link>
          <Link href="/knowledge/health">健康与缺口看板</Link>
          {(["search", "answer", "review"] as const).map((item) => (
            <button key={item} className={tab === item ? "primary" : ""} onClick={() => setTab(item)}>
              {item === "search" ? "独立检索" : item === "answer" ? "资料问答" : "审核工作台"}
            </button>
          ))}
        </div>
        {tab === "search" && <SearchPanel />}
        {tab === "answer" && <AnswerPanel />}
        {tab === "review" && <ReviewPanel />}
      </div>
    </AppShell>
  );
}

function useScopeFields() {
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

function SearchPanel() {
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

function AnswerPanel() {
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

function ReviewPanel() {
  const [documentId, setDocumentId] = useState("");
  const [generationId, setGenerationId] = useState("");
  const [review, setReview] = useState<GenerationReview | null>(null);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const load = async () => {
    setError(""); setReview(null);
    try { setReview(await fetchGenerationReview(documentId, generationId)); }
    catch (cause) { setError(String(cause)); }
  };
  const withRevision = async (action: (revision: number) => Promise<unknown>) => {
    if (!review) return;
    try { await action(review.revision); await load(); }
    catch (cause) { setError(String(cause)); }
  };
  const docTypeConfirmed = (review?.metadata.confirmed?.doc_type as string | undefined) ?? "";
  return (
    <section>
      <div className="document-toolbar">
        <label><input value={documentId} onChange={(event) => setDocumentId(event.target.value)} placeholder="文档 ID" /></label>
        <label><input value={generationId} onChange={(event) => setGenerationId(event.target.value)} placeholder="解析代次 ID" /></label>
        <button className="primary" disabled={!documentId || !generationId} onClick={() => void load()}>载入待审代次</button>
      </div>
      {error && <div className="document-operation error">{error}</div>}
      {review && (
        <>
          <div className="document-operation">
            状态 {review.status} · 修订号 {review.revision} · 单元 {review.unit_total}
            {(review.quality_report.flags?.length ?? 0) > 0 && ` · 质量标记：${review.quality_report.flags!.join("、")}`}
          </div>
          <div className="document-toolbar">
            <select defaultValue={docTypeConfirmed} onChange={(event) => setNote(event.target.value)}>
              <option value="">确认资料类型…</option>
              {docTypes.map((type) => <option key={type} value={type}>{type}</option>)}
            </select>
            <button disabled={!note} onClick={() => void withRevision((revision) => confirmMetadata(documentId, generationId, revision, { doc_type: note }))}>确认元数据</button>
            <button className="primary" onClick={() => void publishGeneration(documentId, generationId)}>发布</button>
          </div>
          <div className="knowledge-results">
            {review.units.map((unit) => (
              <UnitRow key={unit.id} unit={unit} onSave={(text) => withRevision((revision) => reviseUnit(documentId, generationId, revision, unit.id, text))} />
            ))}
          </div>
        </>
      )}
    </section>
  );
}

function UnitRow({
  unit,
  onSave,
}: {
  unit: GenerationReview["units"][number];
  onSave: (text: string) => Promise<void>;
}) {
  const [draft, setDraft] = useState(unit.revised_text ?? unit.raw_text);
  const effective = unit.revised_text ?? unit.raw_text;
  return (
    <article className="document-table-row">
      <b>#{unit.ordinal} {unit.unit_type}</b>
      {unit.excluded && <small> 已排除：{unit.exclusion_reason}</small>}
      <textarea value={draft} onChange={(event) => setDraft(event.target.value)} rows={Math.min(8, Math.ceil(effective.length / 60) + 1)} />
      <div>
        <button disabled={draft === effective} onClick={() => void onSave(draft)}>保存修订</button>
        {unit.revised_text && <small> 已有人工修订</small>}
      </div>
    </article>
  );
}
