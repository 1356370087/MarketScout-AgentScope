"use client";
import { docTypes } from "./document-types";
import { useState } from "react";
import { confirmMetadata, fetchGenerationReview, publishGeneration, reviseUnit, type GenerationReview } from "@/lib/knowledge-api";

export function ReviewPanel() {
  const [documentId, setDocumentId] = useState("");
  const [generationId, setGenerationId] = useState("");
  const [review, setReview] = useState<GenerationReview | null>(null);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const [publishing, setPublishing] = useState(false);
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
  const publish = async () => {
    setError(""); setPublishing(true);
    try {
      await publishGeneration(documentId, generationId);
      await load();
    } catch (cause) { setError(String(cause)); }
    finally { setPublishing(false); }
  };
  const docTypeConfirmed = (review?.metadata.confirmed?.doc_type as string | undefined) ?? "";
  return (
    <section>
      <div className="document-toolbar">
        <label><input value={documentId} onChange={(event) => setDocumentId(event.target.value)} placeholder="文档 ID" /></label>
        <label><input value={generationId} onChange={(event) => setGenerationId(event.target.value)} placeholder="解析代次 ID" /></label>
        <button className="primary" disabled={!documentId || !generationId} onClick={() => void load()}>载入待审代次</button>
      </div>
      {error && <div role="alert" className="document-operation error">{error}</div>}
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
            <button className="primary" disabled={publishing || review.status === "published"} onClick={() => void publish()}>{publishing ? "发布中…" : review.status === "published" ? "已发布" : "发布"}</button>
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
