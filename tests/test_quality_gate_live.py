"""Live integration tests for the moved async handoff quality gate.

Runs the REAL Judge model (deepseek-v4-flash via the LiteLLM proxy alias
``if-quality-v1``) against ``evaluate_subagent_handoff`` and the mailbox-time
admission path (``_claim_and_admit_task_updates``). Skipped unless:

- ``QUALITY_GATE_LIVE=1`` opts in, and
- ``LITELLM_BASE_URL`` / ``LITELLM_SERVICE_KEY`` point at a live proxy.

Usage::

    QUALITY_GATE_LIVE=1 \
    LITELLM_SERVICE_KEY=<master-or-service-key> \
    uv run python -m pytest tests/test_quality_gate_live.py -q --basetemp=.tmp/pytest-live
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from open_deep_research.agents import deep_researcher
from open_deep_research.configuration import Configuration
from open_deep_research.quality.contract import AdmissionStatus
from open_deep_research.tasks.coordination import (
    publish_task_update,
    render_lead_update_context,
)
from open_deep_research.tasks.events import EventType
from open_deep_research.tasks.registry import TaskStatus
from open_deep_research.tasks.state import FileTaskStateStore, TaskSnapshot

pytestmark = pytest.mark.skipif(
    os.getenv("QUALITY_GATE_LIVE") != "1"
    or not os.getenv("LITELLM_BASE_URL")
    or not os.getenv("LITELLM_SERVICE_KEY"),
    reason="live model gate test requires QUALITY_GATE_LIVE=1 with LITELLM_BASE_URL/LITELLM_SERVICE_KEY",
)


def _live_config(runs_dir: str) -> dict[str, Any]:
    return {
        "configurable": {
            "model_backend": "litellm",
            "quality_evaluation_enabled": True,
            "quality_evaluation_model": "if-quality-v1",
            "quality_evaluation_rigor": "balanced",
            "quality_evaluation_min_sources": 3,
            "runs_dir": runs_dir,
            "task_state_backend": "file",
            "event_log_enabled": False,
            "query_session_persistence_enabled": False,
            "task_checkpoint_enabled": False,
        },
        "metadata": {"run_id": "run-live-gate"},
    }


def _contract() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "original_query_sha256": "0" * 64,
        "advisory_dimensions": [],
        "requirements": [
            {
                "requirement_id": "COV-01-abc123",
                "kind": "factual",
                "text": "2024年全球新增光伏装机规模与主要贡献国家",
                "source_located": True,
                "source_message_index": 0,
                "source_start": 0,
                "source_end": 40,
            }
        ],
    }


def _strong_handoff() -> dict[str, Any]:
    evidence = [
        {
            "evidence_id": "ev-1",
            "claim": "Global installed solar PV capacity reached roughly 2.2 TW by end-2024.",
            "source_url": "https://www.iea.org/reports/renewables-2024",
            "source_title": "IEA Renewables 2024",
            "supporting_excerpt": "Solar PV capacity worldwide surpassed 2 TW during 2024.",
        },
        {
            "evidence_id": "ev-2",
            "claim": "China contributed more than half of global solar PV additions in 2024.",
            "source_url": "https://ember-energy.org/latest-insights/global-electricity-review-2025/",
            "source_title": "Ember Global Electricity Review 2025",
            "supporting_excerpt": "China accounted for over half of 2024 solar additions.",
        },
        {
            "evidence_id": "ev-3",
            "claim": "The EU and India were the next-largest solar growth markets in 2024.",
            "source_url": "https://www.iea.org/reports/renewables-2024/executive-summary",
            "source_title": "IEA Renewables Executive Summary",
            "supporting_excerpt": "EU and India followed China in deployment growth.",
        },
    ]
    return {
        "task_id": "task-strong",
        "compressed_research": (
            "## Findings\n"
            "- Global installed solar PV capacity reached roughly 2.2 TW by end-2024 [ev-1].\n"
            "- China contributed more than half of global additions in 2024 [ev-2].\n"
            "- EU and India were the next-largest growth markets [ev-3].\n\n"
            "### Coverage Checklist / Coverage 检查清单\n"
            "- COV-01-abc123: supported (capacity scale + leading countries covered)\n\n"
            "### Sources / 来源\n"
            "- [ev-1] IEA Renewables 2024: https://www.iea.org/reports/renewables-2024\n"
            "- [ev-2] Ember: https://ember-energy.org/latest-insights/global-electricity-review-2025/\n"
            "- [ev-3] IEA Executive Summary: https://www.iea.org/reports/renewables-2024/executive-summary\n"
        ),
        "raw_notes": [],
        "evidence_registry": evidence,
        "requirement_ids": ["COV-01-abc123"],
        "metrics": {"sources_read": 3},
    }


def _empty_handoff() -> dict[str, Any]:
    return {
        "task_id": "task-empty",
        "compressed_research": "No admissible evidence was returned for this dimension.",
        "raw_notes": [],
        "evidence_registry": [],
        "requirement_ids": ["COV-01-abc123"],
        "metrics": {"sources_read": 0},
    }


@pytest.mark.asyncio
async def test_live_strong_handoff_is_admitted() -> None:
    """证据充分且覆盖需求的交接应被接纳，且评估器无错误。"""
    assessment = await deep_researcher.evaluate_subagent_handoff(
        "2024 global solar PV additions",
        _strong_handoff(),
        _live_config(".runs-live-gate"),
        coverage_contract=_contract(),
        requirement_ids=["COV-01-abc123"],
    )
    assert assessment.evaluator_error is None, assessment.evaluator_error
    assert assessment.protocol_errors == [], assessment.protocol_errors
    assert assessment.admission_status is not AdmissionStatus.REJECTED
    scores = (
        assessment.relevance,
        assessment.source_quality,
        assessment.evidence_coverage,
        assessment.groundedness,
    )
    assert min(scores) >= 3, scores
    coverage = {
        item.requirement_id: item.status.value
        for item in assessment.requirement_coverage
    }
    assert coverage.get("COV-01-abc123") in {"supported", "partial"}


@pytest.mark.asyncio
async def test_live_empty_handoff_is_rejected_with_reasons() -> None:
    """无证据交接应被确定性/语义硬拒并携带可行动缺口。"""
    assessment = await deep_researcher.evaluate_subagent_handoff(
        "2024 global solar PV additions",
        _empty_handoff(),
        _live_config(".runs-live-gate"),
        coverage_contract=_contract(),
        requirement_ids=["COV-01-abc123"],
    )
    assert assessment.evaluator_error is None, assessment.evaluator_error
    assert assessment.admission_status is AdmissionStatus.REJECTED
    assert assessment.hard_rejection_reasons, "rejection must carry hard reasons"
    payload = deep_researcher._quality_completed_payload(assessment)
    assert payload["decision"] == "rejected"
    assert payload["hard_rejection_reasons"]
    assert set(payload["scores"]) == {
        "relevance",
        "source_quality",
        "evidence_coverage",
        "groundedness",
    }


async def _prepare_run_directory(tmp_path: Path) -> tuple[Configuration, TaskSnapshot]:
    """Create a completed snapshot with a verified artifact and mailbox message."""
    runs_dir = str(tmp_path)
    configurable = Configuration(
        runs_dir=runs_dir,
        task_state_backend="file",
        event_log_enabled=False,
        query_session_persistence_enabled=False,
        task_checkpoint_enabled=False,
    )
    store = FileTaskStateStore(runs_dir)
    artifact_dir = tmp_path / "run-live-gate" / "context" / "artifacts" / "research_tasks"
    artifact_dir.mkdir(parents=True)
    handoff = _strong_handoff()
    content = json.dumps(handoff, ensure_ascii=False).encode("utf-8")
    artifact = artifact_dir / "task-strong.json"
    artifact.write_bytes(content)
    snapshot = TaskSnapshot(
        task_id="task-strong",
        run_id="run-live-gate",
        research_topic="2024 global solar PV additions",
        status=TaskStatus.COMPLETED,
        wave_id="wave-0",
        requirement_ids=["COV-01-abc123"],
        result=handoff,
        result_artifact_path="context/artifacts/research_tasks/task-strong.json",
        result_artifact_sha256=hashlib.sha256(content).hexdigest(),
        metrics={"source_count": 3},
    )
    await store.upsert(snapshot)
    await publish_task_update(configurable, snapshot, EventType.TASK_COMPLETED)
    return configurable, snapshot


class _CapturePublisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, event_type: str, stage: str = "", payload: dict | None = None, **kwargs: Any) -> None:
        self.published.append((event_type, dict(payload or {})))


@pytest.mark.asyncio
async def test_live_bounded_negative_handoff_rejected_with_followups() -> None:
    """空证据的有界否定交接应被真模型门禁拒绝并给出可行动补证方向。"""
    state = {
        "research_topic": "2024-2026年中国动力电池回收政策与市场",
        "requirement_ids": ["COV-03-abc123"],
        "coverage_contract": _contract(),
        "researcher_messages": [],
        "web_research_iterations": [
            {"budget": {"search_calls": 3, "fetch_attempts": 4, "fetched_documents": 0}},
        ],
        "evidence_registry": [],
    }
    compressed = deep_researcher._bounded_negative_compression_handoff(state)
    assessment = await deep_researcher.evaluate_subagent_handoff(
        "2024-2026年中国动力电池回收政策与市场",
        {
            "task_id": "task-negative",
            "compressed_research": compressed,
            "raw_notes": [],
            "evidence_registry": [],
            "requirement_ids": ["COV-01-abc123"],
            "metrics": {"sources_read": 0},
        },
        _live_config(".runs-live-gate"),
        coverage_contract=_contract(),
        requirement_ids=["COV-01-abc123"],
    )
    assert assessment.evaluator_error is None, assessment.evaluator_error
    assert assessment.admission_status is AdmissionStatus.REJECTED
    assert "insufficient_traceable_sources" in (
        assessment.deterministic_checks or {}
    ).get("failures", [])
    assert assessment.hard_rejection_reasons
    actionable = (
        assessment.missing_information
        or assessment.follow_up_tasks
    )
    assert actionable, "rejection must carry actionable gaps for the Supervisor"
    payload = deep_researcher._quality_completed_payload(assessment)
    assert payload["decision"] == "rejected"
    assert payload["missing_information"] or payload["follow_up_tasks"]
    verdict = deep_researcher._admission_verdict("rejected", assessment)
    assert verdict["admission_status"] == "rejected"
    assert verdict["missing_information"] or verdict["follow_up_tasks"]


@pytest.mark.asyncio
async def test_live_mailbox_claim_admits_and_renders_verdict(tmp_path: Path) -> None:
    """端到端：Lead 消费 task_completed 时即评估、写 snapshot、digest 带 verdict。"""
    configurable, snapshot = await _prepare_run_directory(tmp_path)
    config = _live_config(str(tmp_path))
    config["configurable"]["coverage_contract"] = _contract()
    state: dict[str, Any] = {
        "coverage_ledger": {},
        "coverage_contract": _contract(),
        "research_risk_profile": None,
    }
    publisher = _CapturePublisher()

    context, message_ids, consumer_id, admission_update = (
        await deep_researcher._claim_and_admit_task_updates(
            state,
            config,
            configurable=Configuration.from_runnable_config(config),
            publisher=publisher,
            processed_message_ids=set(),
        )
    )

    assert message_ids, "task_completed message must be claimed"
    # FileTaskStateStore hands out fresh instances; re-read the settled state.
    settled = await FileTaskStateStore(str(tmp_path)).get(
        "task-strong", run_id="run-live-gate"
    )
    assert settled is not None
    assert settled.admission_status != "pending", "gate must settle at claim time"
    assert settled.admission_status != "rejected", settled.admission_status
    snapshot.admission_status = settled.admission_status
    outputs = admission_update.get("completed_task_outputs", [])
    assert [o["task_id"] for o in outputs] == ["task-strong"]
    assert admission_update["handoff_assessments"][0]["tool_call_id"] == "task-strong"
    assert admission_update["coverage_ledger"]["COV-01-abc123"]["status"] in {
        "supported",
        "partial",
    }
    assert "Quality Gate:" in context
    assert "### task-strong - COMPLETED" in context
    quality_events = [
        payload
        for event_type, payload in publisher.published
        if event_type == "research.task.completed"
    ]
    assert quality_events and quality_events[0]["admission_status"] != "rejected"
    assert consumer_id

    # 幂等：同一批消息再次进入接纳路径时，已结算任务不得再次评估
    # （生产语义由 admission_status pending 过滤保证，而非消息重投）。
    from types import SimpleNamespace

    replay_messages = [
        SimpleNamespace(
            type="task_completed",
            message_id=message_ids[0],
            payload={"task_id": "task-strong", "snapshot_version": 1},
        )
    ]
    re_admissions = await deep_researcher._admit_completed_tasks_from_messages(
        state,
        config,
        configurable=Configuration.from_runnable_config(config),
        publisher=publisher,
        messages=replay_messages,
    )
    assert len(re_admissions) == 1, "committed decision must reconstruct unjournaled output"
    assert re_admissions[0].admission_status == settled.admission_status
    settled_again = await FileTaskStateStore(str(tmp_path)).get(
        "task-strong", run_id="run-live-gate"
    )
    assert settled_again is not None
    assert settled_again.admission_status == settled.admission_status
    verdict_context = await render_lead_update_context(
        configurable,
        run_id="run-live-gate",
        messages=[SimpleNamespace(
            type="task_completed",
            message_id=message_ids[0],
            payload={"task_id": "task-strong", "snapshot_version": 1},
        )],
        quality_verdicts={},
    )
    assert "### task-strong - COMPLETED" in verdict_context
