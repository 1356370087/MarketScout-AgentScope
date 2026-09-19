/** Knowledge-base retrieval, Q&A and review-bench API client. */

import { apiFetch } from "./api/http";

import type { KnowledgeSearchResponse, KnowledgeAnswerResponse, KnowledgeSearchRequest } from "./contracts/knowledge";
import type { GenerationReview } from "./contracts/documents";

export type { KnowledgeSearchResponse, KnowledgeEvidence, KnowledgeAnswerResponse, KnowledgeSearchRequest } from "./contracts/knowledge";
export type { GenerationReview } from "./contracts/documents";

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
