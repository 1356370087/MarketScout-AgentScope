/** Knowledge-base retrieval, Q&A and review-bench API client. */

import { apiFetch } from "@/lib/api";

export type KnowledgeSearchResponse = {
  query_id: string;
  rerank_completed: boolean;
  results: KnowledgeEvidence[];
  documents: { document_id: string; filename: string; generation_id: string }[];
  diagnostics?: Record<string, unknown>;
};

export type KnowledgeEvidence = {
  segment_id: string;
  document_id: string;
  generation_id: string;
  filename: string;
  text: string;
  context_before?: string;
  context_after?: string;
  relevance?: number | null;
  score: number;
  source_uri: string;
};

export type KnowledgeAnswerResponse = {
  query_id: string;
  status: "answered" | "no_evidence" | "rerank_unavailable" | "citation_error";
  answer?: string | null;
  support?: string;
  message?: string;
  citations?: { marker: string; segment_ids: string[] }[];
  evidence: KnowledgeEvidence[];
};

export type GenerationReview = {
  id: string;
  status: string;
  revision: number;
  unit_total: number;
  units: {
    id: string;
    ordinal: number;
    unit_type: string;
    raw_text: string;
    revised_text: string | null;
    excluded: boolean;
    exclusion_reason: string | null;
    attributes: Record<string, unknown>;
  }[];
  metadata: { suggested?: Record<string, unknown>; confirmed?: Record<string, unknown> };
  quality_report: { flags?: string[]; revision_conflicts?: unknown[] };
};

export type KnowledgeSearchRequest = {
  query: string;
  kb_ids?: string[];
  collection_ids?: string[];
  document_ids?: string[];
  filters?: {
    doc_types?: string[];
    languages?: string[];
    entity_ids?: string[];
    publish_date_start?: string;
    publish_date_end?: string;
  };
  limit?: number;
};

export async function knowledgeSearch(body: KnowledgeSearchRequest) {
  return apiFetch<KnowledgeSearchResponse>("/knowledge/search", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function knowledgeAnswer(body: KnowledgeSearchRequest) {
  return apiFetch<KnowledgeAnswerResponse>("/knowledge/answer", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function fetchGenerationReview(documentId: string, generationId: string) {
  return apiFetch<GenerationReview>(
    `/documents/${encodeURIComponent(documentId)}/generations/${encodeURIComponent(generationId)}`,
  );
}

export async function confirmMetadata(
  documentId: string,
  generationId: string,
  revision: number,
  metadataConfirmed: Record<string, string>,
) {
  return apiFetch<{ revision: number; applied: Record<string, number> }>(
    `/documents/${encodeURIComponent(documentId)}/generations/${encodeURIComponent(generationId)}/corrections`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ revision, metadata_confirmed: metadataConfirmed }),
    },
  );
}

export async function reviseUnit(
  documentId: string,
  generationId: string,
  revision: number,
  unitId: string,
  revisedText: string,
) {
  return apiFetch<{ revision: number; applied: Record<string, number> }>(
    `/documents/${encodeURIComponent(documentId)}/generations/${encodeURIComponent(generationId)}/corrections`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        revision,
        unit_corrections: [{ unit_id: unitId, revised_text: revisedText }],
      }),
    },
  );
}

export async function publishGeneration(documentId: string, generationId: string) {
  return apiFetch<GenerationReview>(
    `/documents/${encodeURIComponent(documentId)}/generations/${encodeURIComponent(generationId)}/publish`,
    { method: "POST" },
  );
}

export async function submitQueryFeedback(queryId: string, kind: string, note = "") {
  return apiFetch<{ query_id: string; status: string }>("/knowledge/feedback", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ query_id: queryId, kind, note }),
  });
}
