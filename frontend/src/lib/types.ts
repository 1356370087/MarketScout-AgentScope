export type RunStatus =
  | "pending" | "running" | "awaiting_clarification" | "awaiting_plan_approval"
  | "awaiting_outline_approval" | "awaiting_fetch_budget_approval" | "cancelling" | "completed" | "failed" | "cancelled";

export type ConnectionState = "idle" | "connecting" | "connected" | "reconnecting" | "closed" | "error";
export type StageId = "preparing" | "planning" | "researching" | "synthesizing" | "writing" | "finalizing";

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
}
export interface DocumentChunk {
  id: string;
  document_id: string;
  ordinal: number;
  locator: string;
  heading?: string | null;
  text: string;
}

export interface PublicEvent {
  schema_version: 1 | 2 | number;
  event_id: string;
  sequence: number;
  run_id: string;
  type: string;
  timestamp: string;
  stage?: StageId;
  payload: Record<string, unknown>;
}

export interface ResearchTask {
  task_id: string;
  wave_id?: string;
  title?: string;
  status?: string;
  phase?: string;
  iteration?: number;
  source_count?: number;
  elapsed_ms?: number;
  mode?: string;
  activity_phase?: TaskActivityPhase;
  activity_label?: string;
  last_activity_at?: string;
  activity_event_count?: number;
  model_call_count?: number;
  tool_call_count?: number;
  retry_count?: number;
  warning_count?: number;
  activity_available?: boolean;
}

export type TaskActivityKind = "lifecycle" | "model" | "tool" | "source" | "quality" | "checkpoint" | "control" | "security" | "error";
export type TaskActivityPhase = "queued" | "initializing" | "reasoning" | "tool_execution" | "evidence_review" | "quality_check" | "gap_recovery" | "compressing" | "handoff" | "terminal";
export type TaskActivityStatus = "pending" | "running" | "success" | "warning" | "error" | "cancelled";

export interface TaskActivityEvent {
  schema_version: number;
  event_id: string;
  sequence: number;
  run_id: string;
  task_id: string;
  timestamp: string;
  type: string;
  kind: TaskActivityKind;
  phase: TaskActivityPhase;
  status: TaskActivityStatus;
  title: string;
  summary: string;
  iteration?: number;
  duration_ms?: number;
  payload: Record<string, unknown>;
}

export interface TaskActivityPage {
  items: TaskActivityEvent[];
  oldest_sequence: number;
  last_event_id: number;
  has_more: boolean;
  detail_level: "summary" | "preview";
  source: "native" | "derived_trace" | "summary_only";
  stream_url: string;
}

export interface ResearchSource {
  source_id: string;
  task_id?: string;
  title?: string;
  domain?: string;
  url: string;
  source_type?: "web" | "local_document" | string;
  document_id?: string;
  chunk_id?: string;
}

export interface PendingHumanAction {
  action_id: string;
  type: "clarification" | "plan_approval" | "outline_approval" | "fetch_budget_approval";
  payload: { question?: string; content_markdown?: string; research_plan?: string; report_outline?: string };
  allowed_actions?: Array<"approve" | "revise" | "answer" | "deny" | "cancel">;
}

export interface SecurityApproval {
  version?: number;
  approval_id: string;
  run_id: string;
  task_id: string;
  fence_token: number;
  kind: "network" | "tool_effect" | "filesystem" | "command" | "mcp_oauth";
  capability: string;
  target: Record<string, unknown>;
  target_fingerprint: string;
  status: "pending" | "resolved" | "expired" | "consumed";
  decision?: "allow_once" | "allow_run" | "deny";
  reason?: string;
  requested_at: number;
  resolved_at?: number | null;
  expires_at: number;
}

export type EgressRuntimeMode = "manual" | "auto" | "open";

export interface EgressModeState {
  version?: number;
  run_id?: string;
  baseline_mode: string;
  run_setting?: string;
  override?: EgressRuntimeMode | string | null;
  effective_mode: string;
  capped?: boolean;
}

export interface EgressClassification {
  fingerprint?: string;
  capability?: string;
  version?: number;
  domain: string;
  port?: number;
  verdict: "allow" | "ask" | "deny";
  category?: string;
  risk_tags?: string[];
  reason?: string;
  stage_used?: string;
  source?: string;
  task_id?: string;
}

export interface EgressTarget {
  target_id: string;
  target: { domain: string; port: number };
  capability: string;
  version: number;
  decision: "allow_run" | "block_run" | "revoke" | null;
  reason: string;
  updated_at: number;
  policy_denied: boolean;
  classification?: { verdict: "allow" | "ask" | "deny"; reason: string; source: string; classified_at: number } | null;
}

export interface EgressState extends EgressModeState {
  can_resolve?: boolean;
  can_interact?: boolean;
  allowed_modes: EgressRuntimeMode[];
  targets: EgressTarget[];
  target_history?: EgressTarget[];
  records: SecurityApproval[];
  health: { revision?: number; calls_used?: number; remaining_calls?: number; max_calls?: number; degraded?: boolean; reason?: string };
}

export interface ModelCatalogEntryInfo {
  name: string;
  base_model?: string | null;
  context_window: number;
  max_output_tokens: number;
  input_cost_per_token: number;
  output_cost_per_token: number;
}

export interface ModelCatalogResponse {
  backend: string;
  models: ModelCatalogEntryInfo[];
  role_aliases: Record<string, string>;
  stale: boolean;
  error?: string | null;
}

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

export interface CapabilitiesResponse {
  public_event_schema_version?: number;
  public_task_activity_schema_version?: number;
  accepted_event_schema_versions?: number[];
  editable_config_keys: string[];
  defaults: Record<string, unknown>;
  config_schema: {
    type?: string;
    additionalProperties?: boolean;
    properties: Record<string, {
      type?: string;
      title?: string;
      description?: string;
      default?: unknown;
      minimum?: number;
      maximum?: number;
      enum?: unknown[];
      anyOf?: Array<{ type?: string; title?: string; minimum?: number; maximum?: number; enum?: unknown[] }>;
    }>;
    $defs?: Record<string, Record<string, unknown>>;
  };
  features?: Record<string, unknown>;
  publication_theme_defaults?: PublicationTheme;
  [key: string]: unknown;

}

export interface ResearchRunState {
  runId: string;
  title: string;
  status: RunStatus;
  connectionState: ConnectionState;
  currentStage?: StageId;
  stageProgress: Record<string, "pending" | "running" | "completed" | "failed">;
  plan: Record<string, unknown>;
  wavesById: Record<string, { wave_id: string; status: string; task_ids: string[]; mode?: string }>;
  tasksById: Record<string, ResearchTask>;
  sourcesById: Record<string, ResearchSource>;
  findingsByTaskId: Record<string, { task_id: string; summary?: string; sources?: unknown[]; updatedAt: string }>;
  pendingHumanAction?: PendingHumanAction;
  pendingSecurityApprovals: SecurityApproval[];
  egressMode?: EgressModeState;
  egressClassifications: EgressClassification[];
  report: string;
  artifacts: Artifact[];
  publications: PublicationJob[];
  preferredOutputFormat?: string | null;
  publicationTheme?: PublicationTheme;
  qualityGate?: Record<string, unknown>;
  resultStatus?: string;
  terminationReason?: string;
  reportReview?: ReportReviewSummary;
  reportReviewHistory: ReportReviewSummary[];
  reportRevisionCount: number;
  warnings: Array<{ code: string; message: string }>;
  diagnostics: string[];
  lastEventId: number;
  isHydrated: boolean;
  isReconnecting: boolean;
  terminal: boolean;
}

export interface RunSnapshot {
  run_id: string;
  title?: string;
  status: RunStatus;
  pending_human_action?: PendingHumanAction;
  pending_security_approvals?: SecurityApproval[];
  progress?: {
    status?: RunStatus;
    current_stage?: StageId;
    task_items?: Record<string, ResearchTask>;
    sources?: ResearchSource[];
    latest_findings?: Array<Record<string, unknown>>;
    pending_human_action?: PendingHumanAction;
    pending_security_approvals?: SecurityApproval[];
    plan?: Record<string, unknown>;
    last_event_id?: number;
    report_review?: ReportReviewSummary;
    report_review_history?: ReportReviewSummary[];
    report_revision_count?: number;
  };
  output?: {
    markdown?: string;
    artifacts?: Artifact[];
    publications?: PublicationJob[];
    preferred_output_format?: string | null;
    publication_theme?: PublicationTheme;
    quality_gate?: Record<string, unknown>;
    status?: string;
    termination_reason?: string;
    report_review?: ReportReviewSummary | ReportReview;
    report_review_history?: Array<ReportReviewSummary | ReportReview>;
    report_revision_count?: number;
  };

  last_event_id: number;
}

export interface TokenVector {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cached_input_tokens: number;
  cache_creation_input_tokens: number;
  reasoning_tokens: number;
}

export type UsageAccountingStatus = "complete" | "partial" | "unavailable";
export type UsageCostSource = "provider_reported" | "configured_estimate" | "unavailable";

export interface UsageBucket {
  key: string;
  label: string;
  reported: TokenVector;
  estimated: TokenVector;
  call_count: number;
  estimated_cost_micro_usd: number | null;
  cost_source: UsageCostSource;
  average_latency_ms?: number;
  completeness?: UsageAccountingStatus;
}

export interface RunUsageResponse {
  schema_version: 1;
  run_id: string;
  status: string;
  duration_ms: number | null;
  revision: number;
  updated_at: number | null;
  accounting_status: UsageAccountingStatus;
  unavailable_reason?: "no_usage_events" | "run_not_observed" | "accounting_disabled" | "storage_unavailable";
  totals: {
    reported: TokenVector;
    estimated: TokenVector;
    calls: {
      attempts: number;
      successful_responses: number;
      provider_reported: number;
      provider_partial: number;
      estimated: number;
      missing: number;
      unknown_failed_attempts: number;
      legacy_unclassified: number;
      coverage_ratio: number;
    };
    cost: {
      estimated_cost_micro_usd: number | null;
      cost_source: UsageCostSource;
      price_table_hash: string | null;
    };
    budgets: Record<string, { settled: number | null; estimated: number; reserved: number; limit: number | null }>;
  };
  breakdowns: {
    by_stage: UsageBucket[];
    by_agent_role: UsageBucket[];
    by_model: UsageBucket[];
    by_task: UsageBucket[];
    by_stage_gateway?: Array<{
      stage: string;
      calls: number;
      total_tokens: number;
      spend_micro_usd: number;
    }>;
  };
  timeline: Array<{
    timestamp: number;
    reported_tokens: number;
    estimated_tokens: number;
    reported_cumulative: number;
    estimated_cumulative: number;
    call_count: number;
    retry_count: number;
  }>;
  operations: {
    llm_call_count: number;
    retry_count: number;
    rate_limited_count: number;
    rate_429: number;
    cache_hit_rate: number;
    cache_input_ratio: number;
    reasoning_output_ratio: number;
    output_tokens_per_second: number;
    tool_call_count: number;
    tool_success_rate: number;
    empty_tool_result_count: number;
    zero_source_search_count: number;
  };
}

export interface UsageAnalyticsResponse {
  schema_version: 1;
  range: "7d" | "30d" | "retained";
  timezone: string;
  retention_days: number;
  actual_range_days: number;
  summary: {
    run_count: number;
    reported: TokenVector;
    estimated: TokenVector;
    estimated_cost_micro_usd: number | null;
    coverage_ratio: number;
    gateway_spend_micro_usd?: number | null;
    gateway_attributed_runs?: number | null;
    gateway_status?: "ok" | "unavailable";
  };
  daily: Array<{
    date: string;
    reported_tokens: number;
    estimated_tokens: number;
    run_count: number;
    coverage_ratio: number;
    rate_429: number;
    cache_hit_rate: number;
    output_tokens_per_second: number;
  }>;
  distributions: {
    provider: Array<{ key: string; reported_tokens: number; estimated_tokens: number; call_count: number }>;
    model: Array<{ key: string; reported_tokens: number; estimated_tokens: number; call_count: number }>;
    status: Array<{ key: string; reported_tokens: number; estimated_tokens: number; run_count: number }>;
  };
  runs: Array<{
    run_id: string;
    title: string;
    status: string;
    started_at: number;
    ended_at: number | null;
    duration_ms: number | null;
    accounting_status: UsageAccountingStatus;
    reported: TokenVector;
    estimated: TokenVector;
    calls: RunUsageResponse["totals"]["calls"];
    cost: RunUsageResponse["totals"]["cost"];
    operations: RunUsageResponse["operations"];
  }>;
  next_cursor: string | null;
}
