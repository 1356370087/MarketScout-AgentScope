
export interface Artifact { name?: string; type?: string; url?: string; path?: string; content?: unknown }


export type PublicationFormat = "markdown" | "json" | "pdf" | "docx" | "pptx" | "one_pager";
export type PublicationRequestFormat = PublicationFormat | "slides" | "structured_json";
export type PublicationStatus = "queued" | "running" | "completed" | "failed";

export interface PublicationTheme {
  preset: "default" | "boardroom" | "academic";
  primary_color: string;
  accent_color: string;
  font_family: "cjk_sans" | "sans" | "serif";
  locale: "zh-CN" | "en-US";
  footer_text: string;
  pdf_page_size: "a4" | "letter";
  pptx_aspect_ratio: "16:9" | "4:3";
}

export interface PublicationArtifact {
  filename: string;
  media_type: string;
  size_bytes: number;
  sha256: string;
  page_count?: number | null;
  slide_count?: number | null;
  preview?: Record<string, unknown> | null;
  download_url?: string | null;
}

export interface PublicationJob {
  publication_id: string;
  run_id: string;
  requested_format: string;
  format: PublicationFormat;
  status: PublicationStatus;
  report_sha256: string;
  theme: PublicationTheme;
  theme_sha256: string;
  attempt: number;
  max_attempts: number;
  retryable: boolean;
  error_code?: string | null;
  artifact?: PublicationArtifact | null;
  status_url: string;
  events_url: string;
  download_url?: string | null;
  created_at: number;
  updated_at: number;
  started_at?: number | null;
  completed_at?: number | null;
  reused?: boolean;
}

export interface PublicationListResponse {
  run_id: string;
  items: PublicationJob[];
  events_url: string;
  default_theme?: PublicationTheme;
  worker: "ready" | "degraded";
}

export interface PublicationEvent {
  schema_version: 1 | number;
  event_id: string;
  sequence: number;
  run_id: string;
  publication_id: string;
  type: string;
  timestamp: number;
  payload: Record<string, unknown>;
}

/**
 * Client-facing projection of the final-report Reviewer/Revisor loop.
 *
 * The server intentionally exposes only a bounded summary in run snapshots
 * and public events.  The optional fields keep the client forward-compatible
 * with older snapshots and with deployments that do not enable review.
 */
export type ReportReviewDecision = "pass" | "revise" | "fail";
export type ReportReviewStatus =
  | "pending"
  | "running"
  | "revising"
  | "passed"
  | "failed"
  | "degraded"
  | "skipped"
  | "completed"
  | "revised"
  | "error"
  | string;
export type ReportReviewIssueCategory =
  | "coverage"
  | "citation_correctness"
  | "contradiction"
  | "unsupported_claim"
  | "redundancy"
  | "executive_readability"
  | string;
export type ReportReviewIssueSeverity = "info" | "warning" | "major" | "critical" | string;

export interface ReportReviewIssue {
  category: ReportReviewIssueCategory;
  severity: ReportReviewIssueSeverity;
  location?: string | null;
  requirement_id?: string | null;
  evidence_ids?: string[];
  citation_target?: string | null;
  description?: string;
  revision_instruction?: string | null;
}

export interface ReportCoverageReview {
  requirement_id: string;
  status: "covered" | "partial" | "missing" | string;
  explanation?: string;
}

export interface ReportCitationReview {
  claim?: string;
  citation_target?: string | null;
  supported: boolean;
  evidence_ids?: string[];
}

export interface ReportDimensionScores {
  coverage?: number;
  citation_correctness?: number;
  contradictions?: number;
  unsupported_claims?: number;
  redundancy?: number;
  executive_readability?: number;
}

export interface ReportReviewProvenance {
  model?: string;
  policy_version?: string;
  evaluation_epoch?: string | number;
  input_sha256?: string;
  draft_sha256?: string;
}

/** Detailed review shape used internally by the report service. */
export interface ReportReview {
  schema_version?: number | string;
  decision?: ReportReviewDecision | string;
  status?: ReportReviewStatus;
  attempt?: number;
  revision_count?: number;
  dimensions?: ReportDimensionScores;
  coverage?: ReportCoverageReview[];
  citations?: ReportCitationReview[];
  issues?: ReportReviewIssue[];
  issue_count?: number;
  critical_issue_count?: number;
  summary?: string;
  updated_at?: string | number;
  hash?: string;
  review_sha256?: string;
  draft_sha256?: string;
  phase?: string;
  gate_decision?: ReportReviewDecision | string;
  hard_failure?: boolean;
  degraded?: boolean;
  skipped?: boolean;
  provenance?: ReportReviewProvenance;
}

/** Restricted projection sent in a run snapshot and public SSE events. */
export interface ReportReviewSummary {
  schema_version?: number | string;
  status?: ReportReviewStatus;
  decision?: ReportReviewDecision | string | null;
  attempt?: number;
  revision_count?: number;
  issue_count?: number;
  critical_issue_count?: number;
  dimensions?: ReportDimensionScores;
  updated_at?: string | number;
  hash?: string;
  review_sha256?: string;
  draft_sha256?: string;
  policy_version?: string;
  evaluation_epoch?: string | number;
  reason?: string;
  summary?: string;
  phase?: string;
  gate_decision?: ReportReviewDecision | string;
  hard_failure?: boolean;
  degraded?: boolean;
  skipped?: boolean;
}

