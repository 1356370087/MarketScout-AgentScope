
export type SourceMode = "web" | "documents" | "hybrid" | "specific";
export type SourceRef =
  | { type: "document"; id: string }
  | { type: "url"; url: string }
  | { type: "domain"; domain: string };
export interface SourceSelection { mode: SourceMode; sources: SourceRef[] }
export type DocumentStatus = "queued" | "processing" | "ready" | "failed" | "deleting";
export interface ResearchDocument {
  id: string;
  filename: string;
  media_type: string;
  size_bytes: number;
  sha256: string;
  status: DocumentStatus;
  failure_code?: string | null;
  page_count?: number | null;
  chunk_count: number;
  ocr_pages: number;
  created_at: string;
  updated_at: string;
  deleted_at?: string | null;
  current_generation_id?: string | null;
}
export interface DocumentChunk {
  id: string;
  document_id: string;
  ordinal: number;
  locator: string;
  heading?: string | null;
  text: string;
}

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
