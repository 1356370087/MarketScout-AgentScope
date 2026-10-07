import { describe, expect, it } from "vitest";
import { deriveWaveStatus, emptyRunState, hydrateSnapshot, normalizeReportReviewSummary, reducePublicEvent } from "./run-reducer";
import type { PublicEvent } from "./types";

const event = (sequence: number, type: string, payload: Record<string, unknown>): PublicEvent => ({
  schema_version: 2,
  event_id: `e-${sequence}`,
  sequence,
  run_id: "run-1",
  type,
  timestamp: "2026-01-01T00:00:00Z",
  payload,
});

describe("reducePublicEvent", () => {
  it("updates inspection progress through SSE without duplicating a replay", () => {
    const progress = { document_count: 3, processed_chunks: 9, total_chunks: 28,
      candidate_count: 27, admitted_count: 12, counters: {}, tasks: {}, requirements: {} };
    const update = event(3, "research.progress.updated", { progress });
    const state = reducePublicEvent(emptyRunState("run-1"), update);
    expect(state.efficiency).toEqual(progress);
    expect(state.diagnostics).toEqual([]);
    expect(reducePublicEvent(state, update)).toBe(state);
    expect(reducePublicEvent(state, event(2, "research.progress.updated", { progress: {} }))).toBe(state);
  });

  it("preserves partial completion from snapshots and terminal SSE", () => {
    const snapshot = hydrateSnapshot(emptyRunState("run-1"), {
      run_id: "run-1", status: "completed", last_event_id: 287,
      output: {
        status: "partial", termination_reason: "max_turns_drained",
        quality_gate: { status: "degraded", reason_codes: ["handoff_rejected"] },
      },
    });
    expect(snapshot.resultStatus).toBe("partial");
    expect(snapshot.terminationReason).toBe("max_turns_drained");
    const streamed = reducePublicEvent(emptyRunState("run-1"), event(287, "run.completed", {
      status: "completed", result_status: "partial", termination_reason: "max_turns_drained",
    }));
    expect(streamed.resultStatus).toBe("partial");
    expect(streamed.terminationReason).toBe("max_turns_drained");
    expect(streamed.status).toBe("completed");
  });

  it("hydrates a completed snapshot with a closed connection and completed stages", () => {
    const state = hydrateSnapshot(emptyRunState("run-1"), {
      run_id: "run-1", status: "completed", last_event_id: 9,
      progress: { current_stage: "finalizing" }, output: { markdown: "# Report" },
    });
    expect(state.connectionState).toBe("closed");
    expect(state.terminal).toBe(true);
    expect(state.stageProgress).toEqual(Object.fromEntries(["preparing", "planning", "researching", "synthesizing", "writing", "finalizing"].map((stage) => [stage, "completed"])));
  });

  it("derives a wave status when the snapshot only contains task projections", () => {
    expect(deriveWaveStatus([{ task_id: "t1", status: "running" }])).toBe("running");
    expect(deriveWaveStatus([{ task_id: "t1", status: "completed" }, { task_id: "t2", status: "completed" }])).toBe("completed");
    expect(deriveWaveStatus([{ task_id: "t1", status: "failed" }])).toBe("failed");
  });

  it("ignores repeated or older sequences", () => {
    const state = reducePublicEvent(emptyRunState("run-1"), event(2, "run.started", { status: "running" }));
    expect(reducePublicEvent(state, event(2, "run.failed", { status: "failed" }))).toBe(state);
  });

  it("upserts tasks and sources by stable identity", () => {
    let state = reducePublicEvent(emptyRunState("run-1"), event(1, "research.task.started", { task_id: "t1", status: "running" }));
    state = reducePublicEvent(state, event(2, "research.task.progress", { task_id: "t1", iteration: 2, source_count: 10 }));
    state = reducePublicEvent(state, event(3, "research.source.discovered", { task_id: "t1", source_id: "s1", url: "https://example.com/a?x=1" }));
    state = reducePublicEvent(state, event(4, "research.source.discovered", { task_id: "t1", source_id: "s2", url: "https://example.com/a?x=2" }));
    state = reducePublicEvent(state, event(5, "research.task.completed", { task_id: "t1", status: "completed", source_count: 0 }));
    expect(Object.keys(state.tasksById)).toEqual(["t1"]);
    expect(state.tasksById.t1.iteration).toBe(2);
    expect(state.tasksById.t1.source_count).toBe(10);
    expect(Object.keys(state.sourcesById)).toHaveLength(1);
  });

  it("restores and resolves clarification", () => {
    let state = reducePublicEvent(emptyRunState("run-1"), event(1, "clarification.required", { action_id: "a1", question: "范围？", allowed_actions: ["answer", "cancel"] }));
    expect(state.status).toBe("awaiting_clarification");
    expect(state.pendingHumanAction?.payload.question).toBe("范围？");
    state = reducePublicEvent(state, event(2, "clarification.resolved", { action_id: "a1", action: "answer" }));
    expect(state.pendingHumanAction).toBeUndefined();
  });

  it("tracks multiple security approvals independently", () => {
    let state = reducePublicEvent(emptyRunState("run-1"), event(1, "security.approval.required", {
      approval_id: "sec-1", task_id: "t1", kind: "network", capability: "tool.egress", target: { domain: "a.example" }, status: "pending",
    }));
    state = reducePublicEvent(state, event(2, "security.approval.required", {
      approval_id: "sec-2", task_id: "t2", kind: "command", capability: "shell.execute", target: { command: "pytest" }, status: "pending",
    }));
    expect(state.pendingSecurityApprovals.map((item) => item.approval_id)).toEqual(["sec-1", "sec-2"]);
    state = reducePublicEvent(state, event(3, "security.approval.resolved", { approval_id: "sec-1", decision: "allow_once", status: "resolved" }));
    expect(state.pendingSecurityApprovals.map((item) => item.approval_id)).toEqual(["sec-2"]);
  });


  it("tracks egress classifier verdicts keyed by exact target without diagnostics", () => {
    let state = reducePublicEvent(emptyRunState("run-1"), event(1, "security.egress_classified", {
      domain: "docs.example.com", port: 443, verdict: "allow", category: "official_docs", reason: "docs host",
    }));
    state = reducePublicEvent(state, event(2, "security.egress_classified", {
      domain: "news.example.org", verdict: "deny", reason: "phishing pattern",
    }));
    state = reducePublicEvent(state, event(3, "security.egress_classified", {
      domain: "docs.example.com", port: 443, verdict: "allow", category: "official_docs", reason: "docs host updated",
    }));
    expect(state.egressClassifications.map((item) => item.domain)).toEqual(["news.example.org", "docs.example.com"]);
    expect(state.egressClassifications[1].verdict).toBe("allow");
    expect(state.diagnostics).toHaveLength(0);
  });

  it("applies runtime egress mode switches from events", () => {
    let state = reducePublicEvent(emptyRunState("run-1"), event(1, "security.egress_mode_changed", {
      mode: "manual", effective_mode: "manual", previous_mode: "auto", actor: "user-1", origin: "api",
    }));
    expect(state.egressMode?.effective_mode).toBe("manual");
    state = reducePublicEvent(state, event(2, "security.egress_mode_changed", { mode: "auto", effective_mode: "auto" }));
    expect(state.egressMode?.effective_mode).toBe("auto");
    expect(state.egressMode?.override).toBe("auto");
  });

  it("closes on a terminal event without failing on unknown v1 events", () => {
    let state = reducePublicEvent(emptyRunState("run-1"), { ...event(1, "future.event", {}), schema_version: 1 });
    expect(state.diagnostics).toHaveLength(1);
    state = reducePublicEvent(state, event(2, "run.completed", { status: "completed" }));
    expect(state.terminal).toBe(true);
    expect(state.connectionState).toBe("closed");
  });

  it("recognizes model circuit events without unknown diagnostics", () => {
    const opened = reducePublicEvent(emptyRunState("run-1"), event(1, "model.circuit_state", {
      provider: "openai", model: "gpt-test", from_state: "closed", to_state: "open",
    }));
    expect(opened.diagnostics).toHaveLength(0);
    expect(opened.warnings.at(-1)?.code).toBe("model_circuit_open");
    const recovered = reducePublicEvent(opened, event(2, "model.circuit_state", {
      provider: "openai", model: "gpt-test", from_state: "half_open", to_state: "closed",
    }));
    expect(recovered.diagnostics).toHaveLength(0);
  });

  it("hydrates the restricted report review summary without merging it into the research quality gate", () => {
    const state = hydrateSnapshot(emptyRunState("run-1"), {
      run_id: "run-1", status: "completed", last_event_id: 9,
      output: {
        markdown: "# Report",
        quality_gate: { status: "passed" },
        report_review: {
          status: "passed", decision: "pass", attempt: 2, revision_count: 1,
          issue_count: 0, critical_issue_count: 0,
          dimensions: { coverage: 0.98, citation_correctness: 1 }, hash: "abc",
        },
      },
    });
    expect(state.qualityGate).toEqual({ status: "passed" });
    expect(state.reportReview?.decision).toBe("pass");
    expect(state.reportReview?.dimensions?.coverage).toBe(0.98);
    expect(state.reportRevisionCount).toBe(1);
  });

  it("tracks report review and revision events monotonically and ignores duplicate history entries", () => {
    let state = emptyRunState("run-1");
    state = reducePublicEvent(state, event(1, "report.review.started", { attempt: 1 }));
    expect(state.reportReview?.status).toBe("running");
    state = reducePublicEvent(state, event(2, "report.review.completed", {
      attempt: 1, decision: "revise", status: "completed", issue_count: 3, critical_issue_count: 1,
      dimensions: { coverage: 0.7 }, draft_sha256: "draft-1",
    }));
    expect(state.reportReview?.decision).toBe("revise");
    expect(state.reportReviewHistory).toHaveLength(1);
    state = reducePublicEvent(state, event(3, "report.revision.started", { revision_count: 1 }));
    expect(state.reportReview?.status).toBe("revising");
    state = reducePublicEvent(state, event(4, "report.revision.completed", {
      revision_count: 1, status: "revised", issue_count: 0, critical_issue_count: 0,
      dimensions: { coverage: 0.95 }, draft_sha256: "draft-2",
    }));
    expect(state.reportRevisionCount).toBe(1);
    expect(state.reportReviewHistory).toHaveLength(1);
    state = reducePublicEvent(state, event(5, "report.revision.completed", {
      revision_count: 1, status: "revised", issue_count: 0, critical_issue_count: 0,
      dimensions: { coverage: 0.95 }, draft_sha256: "draft-2",
    }));
    expect(state.reportReviewHistory).toHaveLength(1);
    expect(state.diagnostics).toHaveLength(0);
  });

  it("maps semantic review failures and interrupted revisions to failed status", () => {
    let state = reducePublicEvent(emptyRunState("run-1"), event(1, "report.review.completed", {
      attempt: 1, decision: "fail", status: "completed", issue_count: 1,
    }));
    expect(state.reportReview?.status).toBe("failed");

    state = reducePublicEvent(state, event(2, "report.revision.started", {
      attempt: 2, revision_count: 1, status: "running",
    }));
    expect(state.reportReview?.status).toBe("revising");
    state = reducePublicEvent(state, event(3, "run.failed", { status: "failed" }));
    expect(state.reportReview?.status).toBe("failed");
    expect(state.terminal).toBe(true);
  });

  it("normalizes nested and aliased report review fields", () => {
    expect(normalizeReportReviewSummary({
      report_review: { decision: "pass", review_attempt: 3, revision: 2, issues_count: 0, sha256: "hash-1", scores: { redundancy: 0.9 } },
    })).toEqual({
      decision: "pass", attempt: 3, revision_count: 2, issue_count: 0, hash: "hash-1",
      dimensions: { redundancy: 0.9 },
    });
  });

  it("does not let a stale review snapshot roll back a newer SSE attempt", () => {
    let state = reducePublicEvent(emptyRunState("run-1"), event(1, "report.review.started", { attempt: 2, revision_count: 1 }));
    state = reducePublicEvent(state, event(2, "report.review.completed", { attempt: 2, decision: "pass", status: "passed", revision_count: 1 }));
    const hydrated = hydrateSnapshot(state, {
      run_id: "run-1", status: "running", last_event_id: 1,
      output: { report_review: { attempt: 1, decision: "revise", status: "completed", revision_count: 0 } },
    });
    expect(hydrated.reportReview?.attempt).toBe(2);
    expect(hydrated.reportReview?.decision).toBe("pass");
    expect(hydrated.reportRevisionCount).toBe(1);
  });

  it("includes an explicit empty review field so a shallow store reset clears it", () => {
    expect(emptyRunState("run-new")).toHaveProperty("reportReview", undefined);
  });

});
