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
