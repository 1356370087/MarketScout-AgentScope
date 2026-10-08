import { isSecurityApprovalResolved, resolvedSecurityApprovalIds, resolvedHumanActionIds } from "./security-approvals";
import type { PublicEvent, ResearchEfficiency, ResearchRunState, ResearchSource, ResearchTask, RunSnapshot, RunStatus, StageId } from "./contracts/research";
import type { ReportReviewSummary } from "./contracts/publications";

export const STAGES: StageId[] = ["preparing", "planning", "researching", "synthesizing", "writing", "finalizing"];
const TERMINAL = new Set(["run.completed", "run.failed", "run.cancelled"]);
const REPORT_REVIEW_EVENTS = new Set([
  "report.review.started",
  "report.review.completed",
  "report.revision.started",
  "report.revision.completed",
]);

type UnknownRecord = Record<string, unknown>;

function asRecord(value: unknown): UnknownRecord | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as UnknownRecord
    : undefined;
}

function finiteNumber(value: unknown): number | undefined {
  if (typeof value !== "number" || !Number.isFinite(value)) return undefined;
  return value;
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

function normalizeDimensions(value: unknown): ReportReviewSummary["dimensions"] {
  const record = asRecord(value);
  if (!record) return undefined;
  const dimensions: NonNullable<ReportReviewSummary["dimensions"]> = {};
  for (const key of [
    "coverage",
    "citation_correctness",
    "contradictions",
    "unsupported_claims",
    "redundancy",
    "executive_readability",
  ] as const) {
    const score = finiteNumber(record[key]);
    if (score !== undefined) dimensions[key] = score;
  }
  return Object.keys(dimensions).length > 0 ? dimensions : undefined;
}

/** Normalize the restricted wire projection used by snapshots and SSE. */
export function normalizeReportReviewSummary(
  value: unknown,
  defaultStatus?: ReportReviewSummary["status"],
): ReportReviewSummary | undefined {
  const outer = asRecord(value);
  if (!outer) return undefined;
  // Some deployments wrap the projection under `report_review` or `review`.
  // Keep top-level event metadata as a fallback when the nested projection is
  // intentionally sparse (for example, an event carrying only `attempt`).
  const nested = asRecord(outer.report_review) ?? asRecord(outer.review);
  const source = nested ? { ...outer, ...nested } : outer;
  const result: ReportReviewSummary = {};
  const schemaVersion = finiteNumber(source.schema_version);
  const attempt = finiteNumber(source.attempt ?? source.review_attempt);
  const revisionCount = finiteNumber(source.revision_count ?? source.revision ?? source.revisions);
  const issueCount = finiteNumber(source.issue_count ?? source.issues_count)
    ?? (Array.isArray(source.issues) ? source.issues.length : undefined);
  const criticalIssueCount = finiteNumber(source.critical_issue_count ?? source.critical_issues_count ?? source.critical_count ?? source.critical_issues);
  if (schemaVersion !== undefined) result.schema_version = schemaVersion;
  else if (typeof source.schema_version === "string" && source.schema_version.length > 0) result.schema_version = source.schema_version;
  const status = stringValue(source.status) ?? stringValue(source.review_status) ?? defaultStatus;
  if (status) result.status = status;
  if (source.decision !== undefined && source.decision !== null) result.decision = String(source.decision);
  if (attempt !== undefined) result.attempt = attempt;
  if (revisionCount !== undefined) result.revision_count = revisionCount;
  if (issueCount !== undefined) result.issue_count = issueCount;
  if (criticalIssueCount !== undefined) result.critical_issue_count = criticalIssueCount;
  const dimensions = normalizeDimensions(source.dimensions ?? source.dimension_scores ?? source.scores);
  if (dimensions) result.dimensions = dimensions;
  if (typeof source.updated_at === "string" || typeof source.updated_at === "number") result.updated_at = source.updated_at;
  const hash = stringValue(source.hash)
    ?? stringValue(source.review_sha256)
    ?? stringValue(source.sha256)
    ?? stringValue(source.draft_sha256);
  if (hash) result.hash = hash;
  const reviewHash = stringValue(source.review_sha256);
  if (reviewHash) result.review_sha256 = reviewHash;
  const draftHash = stringValue(source.draft_sha256);
  if (draftHash) result.draft_sha256 = draftHash;
  const policyVersion = stringValue(source.policy_version);
  if (policyVersion) result.policy_version = policyVersion;
  if (typeof source.evaluation_epoch === "string" || typeof source.evaluation_epoch === "number") result.evaluation_epoch = source.evaluation_epoch;
  const reason = stringValue(source.reason);
  if (reason) result.reason = reason;
  const summary = stringValue(source.summary) ?? stringValue(source.message);
  if (summary) result.summary = summary;
  const phase = stringValue(source.phase);
  if (phase) result.phase = phase;
  const gateDecision = stringValue(source.gate_decision);
  if (gateDecision) result.gate_decision = gateDecision;
  for (const key of ["hard_failure", "degraded", "skipped"] as const) {
    if (typeof source[key] === "boolean") result[key] = source[key];
  }
  return Object.keys(result).length > 0 ? result : undefined;
}

function mergeReportReviewSummary(
  previous: ReportReviewSummary | undefined,
  incoming: ReportReviewSummary,
): ReportReviewSummary {
  if (
    previous
    && ((incoming.attempt !== undefined && previous.attempt !== undefined && incoming.attempt < previous.attempt)
      || (incoming.revision_count !== undefined && previous.revision_count !== undefined && incoming.revision_count < previous.revision_count))
  ) {
    return previous;
  }
  const dimensions = previous?.dimensions || incoming.dimensions
    ? { ...previous?.dimensions, ...incoming.dimensions }
    : undefined;
  const merged: ReportReviewSummary = { ...previous, ...incoming };
  if (dimensions && Object.keys(dimensions).length > 0) merged.dimensions = dimensions;
  // Attempts and revision counters are monotonic even when a stale snapshot
  // arrives while the SSE stream is reconnecting.
  if (previous?.attempt !== undefined && incoming.attempt !== undefined) merged.attempt = Math.max(previous.attempt, incoming.attempt);
  if (previous?.revision_count !== undefined && incoming.revision_count !== undefined) merged.revision_count = Math.max(previous.revision_count, incoming.revision_count);
  return merged;
}

function appendReviewHistory(history: ReportReviewSummary[], review: ReportReviewSummary): ReportReviewSummary[] {
  const duplicate = history.findIndex((item) => (
    item.attempt === review.attempt
    && item.revision_count === review.revision_count
    && item.status === review.status
    && item.decision === review.decision
    && item.hash === review.hash
  ));
  if (duplicate >= 0) {
    const next = [...history];
    next[duplicate] = mergeReportReviewSummary(next[duplicate], review);
    return next;
  }
  return [...history, review].slice(-20);
}

function reportReviewEventStatus(type: string, payload: UnknownRecord): ReportReviewSummary["status"] {
  if (type === "report.review.started") return "running";
  if (type === "report.revision.started") return "revising";
  if (type === "report.revision.completed") return "revised";
  const decision = stringValue(payload.decision);
  if (decision === "pass") return "passed";
  if (decision === "fail") return "failed";
  return "completed";
}

function normalizeReportReviewEvent(
  type: string,
  payload: UnknownRecord,
): ReportReviewSummary | undefined {
  const incoming = normalizeReportReviewSummary(payload, reportReviewEventStatus(type, payload));
  if (!incoming) return undefined;
  // Older producers used `completed` for every terminal event.  Keep the
  // client status useful when the decision or event kind carries stronger
  // semantics than that generic status.
  if (type === "report.revision.started") {
    incoming.status = "revising";
  } else if (type === "report.review.completed" && incoming.decision === "fail") {
    incoming.status = "failed";
  } else if (
    type === "report.review.completed"
    && incoming.decision === "pass"
    && incoming.status === "completed"
  ) {
    incoming.status = "passed";
  } else if (type === "report.revision.completed" && incoming.status === "completed") {
    incoming.status = "revised";
  }
  return incoming;
}

export function deriveWaveStatus(tasks: ResearchTask[], explicitStatus?: string): string {
  if (explicitStatus) return explicitStatus;
  const statuses = tasks.map((task) => String(task.status ?? task.phase ?? "pending").toLowerCase());
  if (statuses.some((status) => status === "failed")) return "failed";
  if (statuses.length > 0 && statuses.every((status) => status === "completed")) return "completed";
  if (statuses.some((status) => ["running", "researching", "compressing"].includes(status))) return "running";
  return "queued";
}

export function emptyRunState(runId = ""): ResearchRunState {
  return {
    runId, title: runId, status: "pending", connectionState: "idle", stageProgress: {},
    plan: {}, wavesById: {}, tasksById: {}, sourcesById: {}, findingsByTaskId: {},
    pendingSecurityApprovals: [], egressClassifications: [], report: "", artifacts: [], publications: [], preferredOutputFormat: undefined, publicationTheme: undefined, reportReview: undefined,
    reportReviewHistory: [], reportRevisionCount: 0, warnings: [], diagnostics: [], lastEventId: 0,
    isHydrated: false, isReconnecting: false, terminal: false,
  };
}

function sourceKey(source: Partial<ResearchSource>): string {
  try {
    const url = new URL(source.url ?? "");
    url.hash = "";
    url.search = "";
    return url.toString().replace(/\/$/, "").toLowerCase() || source.source_id || "";
  } catch { return source.source_id || source.url || ""; }
}

export function hydrateSnapshot(state: ResearchRunState, snapshot: RunSnapshot): ResearchRunState {
  if (state.isHydrated && state.runId === snapshot.run_id && (snapshot.last_event_id ?? snapshot.progress?.last_event_id ?? 0) < state.lastEventId) return state;
  const progress = snapshot.progress ?? {};
  const terminal = ["completed", "failed", "cancelled"].includes(snapshot.status);
  const stageProgress = snapshot.status === "completed"
    ? Object.fromEntries(STAGES.map((stage) => [stage, "completed" as const]))
    : progress.current_stage && snapshot.status === "failed"
      ? { ...state.stageProgress, [progress.current_stage]: "failed" as const }
      : state.stageProgress;
  const findings = Object.fromEntries((progress.latest_findings ?? []).map((item) => [
    String(item.task_id ?? "unknown"),
    { ...item, task_id: String(item.task_id ?? "unknown"), updatedAt: new Date().toISOString() },
  ]));
  const sources = Object.fromEntries((progress.sources ?? []).map((item) => [sourceKey(item), item]));
  const snapshotReview = normalizeReportReviewSummary(snapshot.output?.report_review ?? progress.report_review);
  const snapshotHistory = (snapshot.output?.report_review_history ?? progress.report_review_history ?? [])
    .map((item) => normalizeReportReviewSummary(item))
    .filter((item): item is ReportReviewSummary => Boolean(item));
  const reportReview = snapshotReview
    ? mergeReportReviewSummary(state.reportReview, snapshotReview)
    : state.reportReview;
  const reportReviewHistory = snapshotHistory.length > 0
    ? snapshotHistory.reduce(appendReviewHistory, state.reportReviewHistory)
    : state.reportReviewHistory;
  const snapshotRevisionCount = finiteNumber(snapshot.output?.report_revision_count ?? progress.report_revision_count);
  const reportRevisionCount = Math.max(
    state.reportRevisionCount,
    snapshotRevisionCount ?? 0,
    reportReview?.revision_count ?? 0,
  );
  return {
    ...state,
    runId: snapshot.run_id,
    title: snapshot.title ?? snapshot.run_id,
    status: snapshot.status,
    connectionState: terminal ? "closed" : state.connectionState,
    currentStage: progress.current_stage ?? undefined,
    stageProgress,
    plan: progress.plan ?? {},
    tasksById: progress.task_items ?? {},
    sourcesById: sources,
    findingsByTaskId: findings,
    pendingHumanAction: resolvedHumanActionIds.has(`${snapshot.run_id}:${(snapshot.pending_human_action ?? progress.pending_human_action)?.action_id}`) ? undefined : snapshot.pending_human_action ?? progress.pending_human_action ?? undefined,
    pendingSecurityApprovals: (snapshot.pending_security_approvals ?? progress.pending_security_approvals ?? []).filter((item) => !isSecurityApprovalResolved(item.approval_id)),
    report: snapshot.output?.markdown ?? "",
    efficiency: snapshot.progress?.efficiency,
    sourcePlan: snapshot.progress?.source_plan,
    completionStatus: snapshot.output?.completion_status ?? undefined,
    stopReason: snapshot.output?.stop_reason ?? undefined,
    researchGaps: snapshot.output?.research_gaps ?? [],
    artifacts: snapshot.output?.artifacts ?? [],
    publications: snapshot.output?.publications ?? [],
    preferredOutputFormat: snapshot.output?.preferred_output_format,
    publicationTheme: snapshot.output?.publication_theme ?? undefined,
    qualityGate: snapshot.output?.quality_gate ?? undefined,
    resultStatus: snapshot.output?.status ?? undefined,
    terminationReason: snapshot.output?.termination_reason ?? undefined,
    reportReview,
    reportReviewHistory,
    reportRevisionCount,
    lastEventId: snapshot.last_event_id ?? progress.last_event_id ?? 0,
    isHydrated: true,
    terminal,
  };
}

export function reducePublicEvent(state: ResearchRunState, event: PublicEvent): ResearchRunState {
  if (event.sequence <= state.lastEventId) return state;
  const payload = event.payload;
  const next: ResearchRunState = { ...state, lastEventId: event.sequence, connectionState: "connected" };
  const status = String(payload.status ?? "") as RunStatus;
  if (event.type.startsWith("run.") && status) next.status = status;
  if (event.type.startsWith("stage.") && payload.stage_id) {
    const stage = String(payload.stage_id) as StageId;
    next.currentStage = stage;
    next.stageProgress = { ...state.stageProgress, [stage]: event.type === "stage.started" ? "running" : event.type.split(".")[1] as "completed" | "failed" };
  } else if (event.type === "plan.created" || event.type === "plan.revised") {
    next.plan = { ...state.plan, ...payload };
  } else if (event.type === "plan.task.added" || event.type.startsWith("research.task.")) {
    const id = String(payload.task_id ?? "");
    if (id) {
      const previous = state.tasksById[id];
      const task = { ...previous, ...payload, task_id: id } as ResearchTask;
      if (typeof previous?.iteration === "number" && typeof task.iteration === "number") task.iteration = Math.max(previous.iteration, task.iteration);
      if (typeof previous?.source_count === "number" && typeof task.source_count === "number") task.source_count = Math.max(previous.source_count, task.source_count);
      next.tasksById = { ...state.tasksById, [id]: task };
    }
  } else if (event.type.startsWith("research.wave.")) {
    const id = String(payload.wave_id ?? "");
    if (id) next.wavesById = { ...state.wavesById, [id]: { ...state.wavesById[id], ...payload, wave_id: id, status: event.type.endsWith("completed") ? "completed" : "running", task_ids: (payload.task_ids as string[]) ?? state.wavesById[id]?.task_ids ?? [] } };
  } else if (event.type === "research.progress.updated") {
    const progress = payload.progress as Record<string, unknown>;
    if ("source_plan" in progress) next.sourcePlan = progress.source_plan as ResearchRunState["sourcePlan"];
    if ("document_count" in progress || "processed_chunks" in progress || "admitted_count" in progress) next.efficiency = progress as unknown as ResearchEfficiency;
  } else if (event.type === "run.usage.updated") {
    // Usage refresh is handled by the stream hook, not an unknown event.
  } else if (event.type === "research.source.discovered") {
    const source = payload as unknown as ResearchSource;
    const key = sourceKey(source);
    if (key) next.sourcesById = { ...state.sourcesById, [key]: { ...state.sourcesById[key], ...source } };
  } else if (event.type === "findings.updated") {
    const id = String(payload.task_id ?? "");
    if (id) next.findingsByTaskId = { ...state.findingsByTaskId, [id]: { ...payload, task_id: id, updatedAt: event.timestamp } };
  } else if (REPORT_REVIEW_EVENTS.has(event.type)) {
    const incoming = normalizeReportReviewEvent(event.type, payload);
    if (incoming) {
      const review = mergeReportReviewSummary(state.reportReview, incoming);
      next.reportReview = review;
      next.reportRevisionCount = Math.max(
        state.reportRevisionCount,
        review.revision_count ?? 0,
        event.type.startsWith("report.revision.") && review.revision_count === undefined
          ? state.reportRevisionCount + (event.type.endsWith("completed") ? 1 : 0)
          : 0,
      );
      if (event.type === "report.review.completed") {
        next.reportReviewHistory = appendReviewHistory(state.reportReviewHistory, review);
      }
    }
  } else if (event.type === "report.completed" && (payload.report_review || payload.review)) {
    const incoming = normalizeReportReviewSummary(payload.report_review ?? payload.review);
    if (incoming) next.reportReview = mergeReportReviewSummary(state.reportReview, incoming);
  } else if (event.type === "approval.required") {
    const kind = `${String(payload.approval_type)}_approval` as "plan_approval" | "outline_approval" | "fetch_budget_approval";
    next.status = `awaiting_${String(payload.approval_type)}_approval` as RunStatus;
    next.pendingHumanAction = { action_id: String(payload.action_id), type: kind, payload: { content_markdown: String(payload.content_markdown ?? ""), source_plan: payload.source_plan as never, version: payload.version as number | undefined, requirements: payload.requirements as never }, allowed_actions: payload.allowed_actions as never };
  } else if (event.type === "clarification.required") {
    next.status = "awaiting_clarification";
    next.pendingHumanAction = { action_id: String(payload.action_id), type: "clarification", payload: { question: String(payload.question ?? "") }, allowed_actions: payload.allowed_actions as never };
  } else if (event.type === "approval.resolved" || event.type === "clarification.resolved") {
    resolvedHumanActionIds.add(`${state.runId}:${payload.action_id}`);
    if (state.pendingHumanAction?.action_id === payload.action_id) next.pendingHumanAction = undefined;
    next.status = "running";
  } else if (event.type === "security.approval.required") {
    const approvalId = String(payload.approval_id ?? "");
    // SSE replays can redeliver a required event after this tab already
    // resolved the approval; session-level resolution wins over the replay.
    if (approvalId && !isSecurityApprovalResolved(approvalId)) next.pendingSecurityApprovals = [...state.pendingSecurityApprovals.filter((item) => item.approval_id !== approvalId), payload as unknown as typeof state.pendingSecurityApprovals[number]];
  } else if (event.type === "security.approval.resolved") {
    const approvalId = String(payload.approval_id ?? "");
    resolvedSecurityApprovalIds.add(approvalId);
    next.pendingSecurityApprovals = state.pendingSecurityApprovals.filter((item) => item.approval_id !== approvalId);
  } else if (event.type === "security.egress_classified") {
    const domain = String(payload.domain ?? "");
    if (domain) {
      const classification = {
        domain,
        fingerprint: typeof payload.fingerprint === "string" ? payload.fingerprint : undefined,
        capability: typeof payload.capability === "string" ? payload.capability : undefined,
        version: Number(payload.version ?? 0),
        port: Number(payload.port ?? 0) || undefined,
        verdict: (payload.verdict === "allow" || payload.verdict === "deny" ? payload.verdict : "ask") as "allow" | "ask" | "deny",
        category: typeof payload.category === "string" ? payload.category : undefined,
        risk_tags: Array.isArray(payload.risk_tags) ? payload.risk_tags.map(String) : undefined,
        reason: typeof payload.reason === "string" ? payload.reason : undefined,
        stage_used: typeof payload.stage_used === "string" ? payload.stage_used : undefined,
        source: typeof payload.source === "string" ? payload.source : undefined,
        task_id: typeof payload.task_id === "string" ? payload.task_id : undefined,
      };
      const same = (item: typeof classification | typeof state.egressClassifications[number]) => classification.fingerprint ? item.fingerprint === classification.fingerprint : item.domain === domain && item.port === classification.port && item.capability === classification.capability;
      const prior = state.egressClassifications.find(same);
      if (!prior || (prior.version ?? 0) <= classification.version) next.egressClassifications = [...state.egressClassifications.filter((item) => !same(item)), classification].slice(-100);
    }
  } else if (event.type === "security.egress_mode_changed") {
    const mode = String(payload.mode ?? "");
    if (mode && Number(payload.version ?? 0) >= (state.egressMode?.version ?? 0)) {
      next.egressMode = {
        ...(state.egressMode ?? { baseline_mode: mode }),
        override: mode,
        effective_mode: String(payload.effective_mode ?? state.egressMode?.effective_mode ?? mode),
        version: Number(payload.version ?? 0),
      };
    }
  } else if (event.type === "security.egress_target_changed" || event.type === "security.egress_health") {
    // The center reloads the authoritative versioned snapshot after this event.
  } else if (event.type === "system.warning") {
    next.warnings = [...state.warnings, { code: String(payload.warning_code ?? "warning"), message: String(payload.message ?? "") }];
  } else if (event.type === "model.circuit_state") {
    if (payload.to_state === "open") {
      next.warnings = [...state.warnings, {
        code: "model_circuit_open",
        message: `Model ${String(payload.provider ?? "unknown")}:${String(payload.model ?? "unknown")} is temporarily isolated.`,
      }];
    }
  } else if (!TERMINAL.has(event.type) && !event.type.startsWith("report.") && !event.type.startsWith("feedback.")) {
    next.diagnostics = [...state.diagnostics, `Unknown event ${event.type} (v${event.schema_version})`].slice(-50);
  }
  if (TERMINAL.has(event.type)) {
    next.terminal = true;
    next.resultStatus = stringValue(payload.result_status) ?? state.resultStatus;
    next.terminationReason = stringValue(payload.termination_reason) ?? state.terminationReason;
    next.pendingHumanAction = undefined;
    next.pendingSecurityApprovals = [];
    next.connectionState = "closed";
    if (event.type === "run.failed" && next.reportReview) {
      const currentStatus = String(next.reportReview.status ?? "").toLowerCase();
      const terminalReview = ["passed", "degraded", "skipped", "failed"].includes(currentStatus)
        && next.reportReview.decision !== "fail";
      if (!terminalReview) {
        next.reportReview = { ...next.reportReview, status: "failed" };
      }
    }
  }
  if (next.pendingHumanAction && resolvedHumanActionIds.has(`${next.runId}:${next.pendingHumanAction.action_id}`)) next.pendingHumanAction = undefined;
  return next;
}
