"""Focused outer-runtime tests for the staged report review lifecycle."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from open_deep_research.agents import deep_researcher as graph
from open_deep_research.agents.query_engine import QueryEngine
from open_deep_research.events.public import event_publisher_from_config
from open_deep_research.report import orchestrator as report_orchestrator
from open_deep_research.report.models import ReportDraft, ReportReview
from open_deep_research.runtime import RuntimeCommand


def _config(tmp_path, run_id: str) -> dict[str, Any]:
    return {
        "configurable": {
            "runs_dir": str(tmp_path),
            "event_log_enabled": False,
            "observability_enabled": False,
            "query_context_compaction_enabled": False,
            "search_api": "none",
            "report_review_enabled": True,
            "report_review_max_revisions": 1,
            "report_review_fail_open": False,
        },
        "metadata": {"run_id": run_id, "owner": "user-1"},
    }


def test_query_gate_keeps_aggregate_only_failure_nonblocking() -> None:
    """The outer gate still requests revision without classifying presentation as critical."""
    decision, issue_count, critical_count, hard_failure = QueryEngine._review_gate(
        {
            "decision": "pass",
            "hard_failure": True,
            "hard_failures": ["score_below_aggregate_floor"],
            "dimensions": {
                "coverage": 1.0,
                "citation_correctness": 1.0,
                "contradictions": 1.0,
                "unsupported_claims": 1.0,
                "redundancy": 0.0,
                "executive_readability": 0.0,
            },
            "quality_thresholds": {
                "outer_critical_floor": 0.7,
                "outer_aggregate_floor": 0.75,
            },
        }
    )

    assert decision == "revise"
    assert issue_count == 0
    assert critical_count == 0
    assert hard_failure is False


def test_report_recovery_artifacts_are_bound_to_source_draft_and_review(
    tmp_path,
) -> None:
    """Stale recovery/revision files are never applied to a different source."""
    config = _config(tmp_path, "report-artifact-source-binding")
    engine = QueryEngine(config)
    assert engine.context_store is not None
    engine.context_store.initialize("user-1", config)

    source_review = {"attempt": 1, "decision": "revise", "draft_sha256": "source-a"}
    source_review_sha256 = engine._report_payload_sha256(source_review)
    revised_text = "# Revised\n\nBound output."
    revised_sha256 = hashlib.sha256(revised_text.encode("utf-8")).hexdigest()
    revised_payload = {
        "markdown": revised_text,
        "body_markdown": revised_text,
        "sha256": revised_sha256,
    }
    engine.context_store.write_text_atomic(
        "report_drafts/revision-001.md",
        revised_text,
    )
    engine.context_store.write_json_atomic(
        "report_drafts/revision-001.json",
        {
            "schema_version": 1,
            "attempt": 1,
            "draft_sha256": revised_sha256,
            "source_draft_sha256": "source-a",
            "source_review_attempt": 1,
            "source_review_sha256": source_review_sha256,
            "draft": revised_payload,
        },
    )

    loaded_text, _ = engine._load_report_revision_artifact(
        1,
        source_draft_sha256="source-a",
        source_review_attempt=1,
        source_review_sha256=source_review_sha256,
    )
    assert loaded_text == revised_text
    (
        tmp_path
        / "report-artifact-source-binding"
        / "context"
        / "report_drafts"
        / "revision-001.md"
    ).unlink()
    metadata_only_text, _ = engine._load_report_revision_artifact(
        1,
        source_draft_sha256="source-a",
        source_review_attempt=1,
        source_review_sha256=source_review_sha256,
    )
    assert metadata_only_text == revised_text
    assert engine._load_report_revision_artifact(
        1,
        source_draft_sha256="source-b",
        source_review_attempt=1,
        source_review_sha256=source_review_sha256,
    ) == ("", None)

    recovery_text = "# Evidence-limited\n\nBound recovery."
    recovery_sha256 = hashlib.sha256(recovery_text.encode("utf-8")).hexdigest()
    recovery_payload = {
        "markdown": recovery_text,
        "body_markdown": recovery_text,
        "sha256": recovery_sha256,
        "provenance": {"report_review_recovery": True},
    }
    engine.context_store.write_text_atomic(
        "report_drafts/evidence-limited-recovery.md",
        recovery_text,
    )
    engine.context_store.write_json_atomic(
        "report_drafts/evidence-limited-recovery.json",
        {
            "schema_version": 1,
            "draft_sha256": recovery_sha256,
            "source_draft_sha256": "source-a",
            "source_review_attempt": 1,
            "source_review_sha256": source_review_sha256,
            "draft": recovery_payload,
        },
    )

    loaded_recovery, _ = engine._load_report_recovery_artifact(
        source_draft_sha256="source-a",
        source_review_attempt=1,
        source_review_sha256=source_review_sha256,
    )
    assert loaded_recovery == recovery_text
    (
        tmp_path
        / "report-artifact-source-binding"
        / "context"
        / "report_drafts"
        / "evidence-limited-recovery.md"
    ).unlink()
    metadata_only_recovery, _ = engine._load_report_recovery_artifact(
        source_draft_sha256="source-a",
        source_review_attempt=1,
        source_review_sha256=source_review_sha256,
    )
    assert metadata_only_recovery == recovery_text
    assert engine._load_report_recovery_artifact(
        source_draft_sha256="source-a",
        source_review_attempt=2,
        source_review_sha256=source_review_sha256,
    ) == ("", None)


def _patch_runtime_shell(monkeypatch) -> None:
    """Install the non-report QueryEngine stages used by crash-window tests."""
    async def summarize(_state, _config):
        return RuntimeCommand(goto="memory_recall")

    async def memory(_state, _config):
        return RuntimeCommand(goto="clarify_with_user")

    async def clarify(_state, _config):
        return RuntimeCommand(goto="write_research_brief")

    async def brief(_state, _config):
        return RuntimeCommand(
            goto="research_supervisor",
            update={
                "research_brief": "Compare options.",
                "supervisor_messages": [SystemMessage(content="supervisor")],
                "enable_async_research": False,
            },
        )

    async def supervisor(_self, _state):
        return {"notes": {"type": "override", "value": ["finding"]}}

    async def write_memory(_state, _config):
        return RuntimeCommand()

    monkeypatch.setattr(graph, "summarize_messages", summarize)
    monkeypatch.setattr(graph, "memory_recall", memory)
    monkeypatch.setattr(graph, "clarify_with_user", clarify)
    monkeypatch.setattr(graph, "write_research_brief", brief)
    monkeypatch.setattr(graph, "memory_extract_and_write", write_memory)
    monkeypatch.setattr(QueryEngine, "_run_supervisor", supervisor)


def _patch_report_stages(monkeypatch, **stages: Any) -> None:
    for name, function in stages.items():
        monkeypatch.setattr(report_orchestrator, name, function)
        monkeypatch.setattr(f"open_deep_research.report.{name}", function)


@pytest.mark.asyncio
async def test_resume_reuses_review_state_without_duplicate_reviewer_call(
    tmp_path,
    monkeypatch,
) -> None:
    """A crash after the review delta resumes at finalize without re-reviewing."""
    _patch_runtime_shell(monkeypatch)
    calls = {"draft": 0, "review": 0, "finalize": 0}

    async def build_draft(_state, _config):
        calls["draft"] += 1
        return ReportDraft(markdown="# Draft\n\nSupported claim [1].")

    async def review(_draft, _state, _config, *, attempt=1):
        calls["review"] += 1
        return ReportReview(
            decision="pass",
            dimensions={
                "coverage": 1,
                "citation_correctness": 1,
                "contradictions": 1,
                "unsupported_claims": 1,
                "redundancy": 1,
                "executive_readability": 1,
            },
            attempt=attempt,
        )

    async def finalize(draft, _state, _config):
        calls["finalize"] += 1
        markdown = draft.markdown if hasattr(draft, "markdown") else draft["markdown"]
        return {"final_report": markdown}

    _patch_report_stages(
        monkeypatch,
        build_report_draft=build_draft,
        review_report=review,
        finalize_report=finalize,
    )
    config = _config(tmp_path, "review-checkpoint-crash")
    first = QueryEngine(config)
    original_checkpoint = first._persist_checkpoint
    interrupted = False

    async def interrupt_after_review(stage, next_stage, **kwargs):
        nonlocal interrupted
        if stage == "report_reviewed" and not interrupted:
            interrupted = True
            raise RuntimeError("crash_after_review_delta")
        await original_checkpoint(stage, next_stage, **kwargs)

    monkeypatch.setattr(first, "_persist_checkpoint", interrupt_after_review)
    failed = await first.submit_message([HumanMessage(content="研究请求")])

    assert failed["result"]["status"] == "error"
    assert calls == {"draft": 1, "review": 1, "finalize": 0}
    resumed = QueryEngine.load(
        "review-checkpoint-crash",
        runs_dir=str(tmp_path),
        config={"metadata": {"owner": "user-1"}},
    )
    result = await resumed.resume()

    assert result["result"]["status"] == "success", result["result"]
    assert calls == {"draft": 1, "review": 1, "finalize": 1}
    assert len(result["report_review_history"]) == 1
    public_events = event_publisher_from_config(resumed.config).store.read()
    assert sum(event.type == "report.completed" for event in public_events) == 1


@pytest.mark.asyncio
async def test_resume_reuses_revision_artifact_without_duplicate_revisor_call(
    tmp_path,
    monkeypatch,
) -> None:
    """A committed revision artifact survives a pre-delta process crash."""
    _patch_runtime_shell(monkeypatch)
    calls = {"draft": 0, "review": 0, "revise": 0, "finalize": 0}

    async def build_draft(_state, _config):
        calls["draft"] += 1
        return ReportDraft(markdown="# Draft\n\nWeak recommendation [1].")

    async def review(_draft, _state, _config, *, attempt=1):
        calls["review"] += 1
        return ReportReview(
            decision="revise" if attempt == 1 else "pass",
            dimensions={
                "coverage": 1,
                "citation_correctness": 1,
                "contradictions": 1,
                "unsupported_claims": 1,
                "redundancy": 1,
                "executive_readability": 0.5 if attempt == 1 else 1,
            },
            issues=(
                [
                    {
                        "category": "executive_readability",
                        "severity": "major",
                        "description": "Make the recommendation explicit.",
                    }
                ]
                if attempt == 1
                else []
            ),
            attempt=attempt,
        )

    async def revise(_draft, _review, _state, _config):
        calls["revise"] += 1
        return "# Revised\n\nClear recommendation [1]."

    async def finalize(draft, _state, _config):
        calls["finalize"] += 1
        markdown = draft.markdown if hasattr(draft, "markdown") else draft["markdown"]
        return {"final_report": markdown}

    _patch_report_stages(
        monkeypatch,
        build_report_draft=build_draft,
        review_report=review,
        revise_report=revise,
        finalize_report=finalize,
    )
    config = _config(tmp_path, "revision-artifact-crash")
    first = QueryEngine(config)
    original_update = first._persist_update
    interrupted = False

    async def interrupt_before_revision_delta(*, channel, stage, update, **kwargs):
        nonlocal interrupted
        if stage == "report_revised" and not interrupted:
            interrupted = True
            raise RuntimeError("crash_before_revision_delta")
        await original_update(
            channel=channel,
            stage=stage,
            update=update,
            **kwargs,
        )

    monkeypatch.setattr(first, "_persist_update", interrupt_before_revision_delta)
    failed = await first.submit_message([HumanMessage(content="研究请求")])

    assert failed["result"]["status"] == "error"
    assert calls == {"draft": 1, "review": 1, "revise": 1, "finalize": 0}
    resumed = QueryEngine.load(
        "revision-artifact-crash",
        runs_dir=str(tmp_path),
        config={"metadata": {"owner": "user-1"}},
    )
    result = await resumed.resume()

    assert result["result"]["status"] == "success", result["result"]
    assert calls == {"draft": 1, "review": 2, "revise": 1, "finalize": 1}
    assert result["report_revision_count"] == 1
    assert len(result["report_review_history"]) == 2
    public_events = event_publisher_from_config(resumed.config).store.read()
    assert sum(event.type == "report.revision.completed" for event in public_events) == 1
    assert sum(event.type == "report.completed" for event in public_events) == 1


@pytest.mark.asyncio
async def test_enabled_query_runtime_publishes_only_reviewed_final_report(
    tmp_path,
    monkeypatch,
) -> None:
    """The outer runtime routes draft -> review -> finalize and persists stages."""
    calls: list[str] = []

    async def summarize(_state, _config):
        return RuntimeCommand(goto="memory_recall")

    async def memory(_state, _config):
        return RuntimeCommand(goto="clarify_with_user")

    async def clarify(_state, _config):
        return RuntimeCommand(goto="write_research_brief")

    async def brief(_state, _config):
        return RuntimeCommand(
            goto="research_supervisor",
            update={
                "research_brief": "Compare options.",
                "supervisor_messages": [SystemMessage(content="supervisor")],
                "enable_async_research": False,
            },
        )

    async def supervisor(_self, _state):
        return {"notes": {"type": "override", "value": ["finding"]}}

    async def build_draft(_state, _config):
        calls.append("draft")
        return ReportDraft(markdown="# Draft\n\nSupported claim [1].")

    async def review(draft, _state, _config, *, attempt=1):
        calls.append(f"review-{attempt}")
        return ReportReview(
            decision="pass",
            dimensions={
                "coverage": 1,
                "citation_correctness": 1,
                "contradictions": 1,
                "unsupported_claims": 1,
                "redundancy": 1,
                "executive_readability": 1,
            },
            attempt=attempt,
            draft_sha256="",
        )

    async def finalize(draft, _state, _config):
        calls.append("finalize")
        return {"final_report": draft.markdown}

    async def write_memory(_state, _config):
        return RuntimeCommand()

    monkeypatch.setattr(graph, "summarize_messages", summarize)
    monkeypatch.setattr(graph, "memory_recall", memory)
    monkeypatch.setattr(graph, "clarify_with_user", clarify)
    monkeypatch.setattr(graph, "write_research_brief", brief)
    monkeypatch.setattr(graph, "memory_extract_and_write", write_memory)
    monkeypatch.setattr(QueryEngine, "_run_supervisor", supervisor)
    monkeypatch.setattr(report_orchestrator, "build_report_draft", build_draft)
    monkeypatch.setattr(report_orchestrator, "review_report", review)
    monkeypatch.setattr(report_orchestrator, "finalize_report", finalize)
    monkeypatch.setattr("open_deep_research.report.build_report_draft", build_draft)
    monkeypatch.setattr("open_deep_research.report.review_report", review)
    monkeypatch.setattr("open_deep_research.report.finalize_report", finalize)

    engine = QueryEngine(_config(tmp_path, "review-runtime"))
    result = await engine.submit_message([HumanMessage(content="研究请求")])

    assert result["result"]["status"] == "success", result["result"]
    assert result["final_report"] == "# Draft\n\nSupported claim [1]."
    assert calls == ["draft", "review-1", "finalize"]
    events = [item["event"] for item in engine.transcript]
    assert events.count("report.completed") == 1
    assert "report.review.completed" in events
    assert "report.revision.started" not in events
    context = tmp_path / "review-runtime" / "context"
    assert (context / "final_draft.md").exists()
    draft_envelope = json.loads(
        (context / "final_draft.json").read_text(encoding="utf-8")
    )
    expected_draft_hash = hashlib.sha256(result["final_report"].encode()).hexdigest()
    assert draft_envelope["attempt"] == 0
    assert draft_envelope["draft_sha256"] == expected_draft_hash
    assert draft_envelope["model"] == engine._report_config_value(
        "final_report_model",
        None,
    )
    assert draft_envelope["policy_version"] == engine.config["metadata"][
        "quality_policy_version"
    ]
    assert draft_envelope["evaluation_epoch"] == engine.config["metadata"][
        "quality_evaluation_epoch"
    ]
    assert (context / "report_reviews" / "attempt-001.json").exists()
    assert (context / "final_report.md").read_text(encoding="utf-8") == result["final_report"]


@pytest.mark.asyncio
async def test_enabled_query_runtime_revises_then_rechecks_before_finalize(
    tmp_path,
    monkeypatch,
) -> None:
    """A revise verdict loops back to Reviewer with the new draft body."""
    calls: list[str] = []

    async def build_draft(_state, _config):
        calls.append("draft")
        return ReportDraft(markdown="# Draft\n\nWeak recommendation [1].")

    async def review(draft, _state, _config, *, attempt=1):
        calls.append(f"review-{attempt}:{draft.markdown}")
        decision = "revise" if attempt == 1 else "pass"
        return ReportReview(
            decision=decision,
            dimensions={
                "coverage": 1,
                "citation_correctness": 1,
                "contradictions": 1,
                "unsupported_claims": 1,
                "redundancy": 1,
                "executive_readability": 0.5 if attempt == 1 else 1,
            },
            issues=(
                [{"category": "executive_readability", "severity": "major", "description": "Make the recommendation explicit."}]
                if attempt == 1
                else []
            ),
            attempt=attempt,
        )

    async def revise(draft, _review, _state, _config):
        calls.append("revise")
        return "# Final\n\nClear recommendation [1]."

    async def finalize(draft, _state, _config):
        calls.append("finalize")
        return {"final_report": draft.markdown}

    async def write_memory(_state, _config):
        return RuntimeCommand()

    async def summarize(_state, _config):
        return RuntimeCommand(goto="memory_recall")

    async def memory(_state, _config):
        return RuntimeCommand(goto="clarify_with_user")

    async def clarify(_state, _config):
        return RuntimeCommand(goto="write_research_brief")

    async def brief(_state, _config):
        return RuntimeCommand(
            goto="research_supervisor",
            update={
                "research_brief": "Compare options.",
                "supervisor_messages": [SystemMessage(content="supervisor")],
                "enable_async_research": False,
            },
        )

    async def supervisor(_self, _state):
        return {"notes": {"type": "override", "value": ["finding"]}}

    monkeypatch.setattr(graph, "summarize_messages", summarize)
    monkeypatch.setattr(graph, "memory_recall", memory)
    monkeypatch.setattr(graph, "clarify_with_user", clarify)
    monkeypatch.setattr(graph, "write_research_brief", brief)
    monkeypatch.setattr(graph, "memory_extract_and_write", write_memory)
    monkeypatch.setattr(QueryEngine, "_run_supervisor", supervisor)
    for name, function in {
        "build_report_draft": build_draft,
        "review_report": review,
        "revise_report": revise,
        "finalize_report": finalize,
    }.items():
        monkeypatch.setattr(report_orchestrator, name, function)
        monkeypatch.setattr(f"open_deep_research.report.{name}", function)

    result = await QueryEngine(_config(tmp_path, "review-revise-runtime")).submit_message(
        [HumanMessage(content="研究请求")]
    )

    assert result["result"]["status"] == "success", result["result"]
    assert result["final_report"] == "# Final\n\nClear recommendation [1]."
    assert calls[0] == "draft"
    assert calls[1].startswith("review-1:")
    assert calls[2] == "revise"
    assert calls[3].startswith("review-2:# Final")
    assert calls[4] == "finalize"


@pytest.mark.asyncio
async def test_query_runtime_rechecks_evidence_limited_recovery_before_publish(
    tmp_path,
    monkeypatch,
) -> None:
    """Critical defects at the limit route through one recovered draft and Reviewer."""
    calls: list[str] = []

    async def summarize(_state, _config):
        return RuntimeCommand(goto="memory_recall")

    async def memory(_state, _config):
        return RuntimeCommand(goto="clarify_with_user")

    async def clarify(_state, _config):
        return RuntimeCommand(goto="write_research_brief")

    async def brief(_state, _config):
        return RuntimeCommand(
            goto="research_supervisor",
            update={
                "research_brief": "Compare options.",
                "supervisor_messages": [SystemMessage(content="supervisor")],
                "enable_async_research": False,
            },
        )

    async def supervisor(_self, _state):
        return {"notes": {"type": "override", "value": ["finding"]}}

    async def write_memory(_state, _config):
        return RuntimeCommand()

    async def build_draft(_state, _config):
        calls.append("draft")
        return ReportDraft(markdown="# Draft\n\nOverstated recommendation [1].")

    async def review(draft, _state, _config, *, attempt=1):
        payload = draft.model_dump(mode="json") if hasattr(draft, "model_dump") else draft
        provenance = payload.get("provenance", {}) if isinstance(payload, dict) else {}
        recovered = bool(
            isinstance(provenance, dict)
            and provenance.get("report_review_recovery")
        )
        calls.append(f"review-{attempt}-{'recovery' if recovered else 'draft'}")
        return ReportReview(
            decision="pass" if recovered else "revise",
            dimensions={
                "coverage": 1,
                "citation_correctness": 1,
                "contradictions": 1,
                "unsupported_claims": 1,
                "redundancy": 1,
                "executive_readability": 1,
            },
            issues=(
                []
                if recovered
                else [
                    {
                        "category": "unsupported_claims",
                        "severity": "critical",
                        "description": "The recommendation exceeds accepted evidence.",
                    }
                ]
            ),
            attempt=attempt,
        )

    async def recover(draft, _state, _config, **_kwargs):
        calls.append("recover")
        payload = draft.model_dump(mode="json") if hasattr(draft, "model_dump") else dict(draft)
        return ReportDraft.model_validate(
            {
                **payload,
                "markdown": "# Evidence-limited\n\nSupported claim [1].",
                "body_markdown": "# Evidence-limited\n\nSupported claim [1].",
                "provenance": {
                    "source": "report_review_evidence_limited_recovery",
                    "report_review_recovery": True,
                },
                "finalization": {
                    "completion_decision": {
                        "type": "override",
                        "value": {
                            "action": "complete_partial",
                            "reason": "report_review_evidence_limited_recovery",
                            "gaps": ["unsupported_claim"],
                        },
                    }
                },
            }
        )

    monkeypatch.setattr(graph, "summarize_messages", summarize)
    monkeypatch.setattr(graph, "memory_recall", memory)
    monkeypatch.setattr(graph, "clarify_with_user", clarify)
    monkeypatch.setattr(graph, "write_research_brief", brief)
    monkeypatch.setattr(graph, "memory_extract_and_write", write_memory)
    monkeypatch.setattr(QueryEngine, "_run_supervisor", supervisor)
    for name, function in {
        "build_report_draft": build_draft,
        "review_report": review,
        "recover_report_draft": recover,
    }.items():
        monkeypatch.setattr(report_orchestrator, name, function)
        monkeypatch.setattr(f"open_deep_research.report.{name}", function)

    config = _config(tmp_path, "review-recovery-runtime")
    config["configurable"]["report_review_max_revisions"] = 0
    result = await QueryEngine(config).submit_message(
        [HumanMessage(content="研究请求")]
    )

    assert result["result"]["status"] == "partial", result["result"]
    assert result["final_report"].startswith("# Evidence-limited")
    assert calls == ["draft", "review-1-draft", "recover", "review-2-recovery"]
    assert result["report_review"]["status"] == "degraded"
    assert result["completion_decision"]["action"] == "complete_partial"


@pytest.mark.asyncio
async def test_query_runtime_recovers_after_revision_without_reusing_revision_hash(
    tmp_path,
    monkeypatch,
) -> None:
    """A terminal recovery after a Revisor pass gets its own final review."""
    calls: list[str] = []

    async def summarize(_state, _config):
        return RuntimeCommand(goto="memory_recall")

    async def memory(_state, _config):
        return RuntimeCommand(goto="clarify_with_user")

    async def clarify(_state, _config):
        return RuntimeCommand(goto="write_research_brief")

    async def brief(_state, _config):
        return RuntimeCommand(
            goto="research_supervisor",
            update={
                "research_brief": "Compare options.",
                "supervisor_messages": [SystemMessage(content="supervisor")],
                "enable_async_research": False,
            },
        )

    async def supervisor(_self, _state):
        return {"notes": {"type": "override", "value": ["finding"]}}

    async def write_memory(_state, _config):
        return RuntimeCommand()

    async def build_draft(_state, _config):
        calls.append("draft")
        return ReportDraft(markdown="# Draft\n\nWeak recommendation [1].")

    async def review(draft, _state, _config, *, attempt=1):
        payload = draft.model_dump(mode="json") if hasattr(draft, "model_dump") else draft
        provenance = payload.get("provenance", {}) if isinstance(payload, dict) else {}
        recovered = bool(
            isinstance(provenance, dict)
            and provenance.get("report_review_recovery")
        )
        label = "recovery" if recovered else "revised" if attempt > 1 else "draft"
        calls.append(f"review-{attempt}-{label}")
        if recovered:
            decision = "pass"
            issues = []
        elif attempt == 1:
            decision = "revise"
            issues = [
                {
                    "category": "executive_readability",
                    "severity": "major",
                    "description": "Make the recommendation explicit.",
                }
            ]
        else:
            decision = "revise"
            issues = [
                {
                    "category": "unsupported_claims",
                    "severity": "critical",
                    "description": "The revised recommendation is unsupported.",
                }
            ]
        return ReportReview(
            decision=decision,
            dimensions={
                "coverage": 1,
                "citation_correctness": 1,
                "contradictions": 1,
                "unsupported_claims": 1,
                "redundancy": 1,
                "executive_readability": 1,
            },
            issues=issues,
            attempt=attempt,
        )

    async def revise(_draft, _review, _state, _config):
        calls.append("revise")
        return "# Revised\n\nOverstated recommendation [1]."

    async def recover(draft, _state, _config, **_kwargs):
        calls.append("recover")
        payload = draft.model_dump(mode="json") if hasattr(draft, "model_dump") else dict(draft)
        return ReportDraft.model_validate(
            {
                **payload,
                "markdown": "# Evidence-limited\n\nSupported claim [1].",
                "body_markdown": "# Evidence-limited\n\nSupported claim [1].",
                "provenance": {
                    "source": "report_review_evidence_limited_recovery",
                    "report_review_recovery": True,
                },
                "finalization": {
                    "completion_decision": {
                        "type": "override",
                        "value": {
                            "action": "complete_partial",
                            "reason": "report_review_evidence_limited_recovery",
                            "gaps": ["unsupported_claim"],
                        },
                    }
                },
            }
        )

    monkeypatch.setattr(graph, "summarize_messages", summarize)
    monkeypatch.setattr(graph, "memory_recall", memory)
    monkeypatch.setattr(graph, "clarify_with_user", clarify)
    monkeypatch.setattr(graph, "write_research_brief", brief)
    monkeypatch.setattr(graph, "memory_extract_and_write", write_memory)
    monkeypatch.setattr(QueryEngine, "_run_supervisor", supervisor)
    for name, function in {
        "build_report_draft": build_draft,
        "review_report": review,
        "revise_report": revise,
        "recover_report_draft": recover,
    }.items():
        monkeypatch.setattr(report_orchestrator, name, function)
        monkeypatch.setattr(f"open_deep_research.report.{name}", function)

    engine = QueryEngine(_config(tmp_path, "review-revision-recovery-runtime"))
    result = await engine.submit_message([HumanMessage(content="研究请求")])

    assert result["result"]["status"] == "partial", result["result"]
    assert calls == [
        "draft",
        "review-1-draft",
        "revise",
        "review-2-revised",
        "recover",
        "review-3-recovery",
    ]
    assert result["report_revision_count"] == 1
    assert len(result["report_review_history"]) == 3
    assert [item["event"] for item in engine.transcript].count(
        "report.revision.completed"
    ) == 1
