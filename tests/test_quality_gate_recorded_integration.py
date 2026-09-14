"""Offline integration replay from the retained 2026-09-06 E2E bundle.

Saved assessments are projected responses, not original raw Judge messages.
No research, search, or external model requests are permitted in this suite.
"""

import copy
import json
import os
import socket
from pathlib import Path

import httpx
import pytest
from openai import AsyncOpenAI

from open_deep_research.agents.query_engine import QueryEngine
from open_deep_research.events.public import event_publisher_from_config
from open_deep_research.models import invocation
from open_deep_research.models.gateway import LiteLLMModelGateway
from open_deep_research.quality import gate
from open_deep_research.quality.contract import merge_coverage_ledger

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def recorded_bundle():
    root = Path(os.getenv("QUALITY_GATE_REPLAY_DIR", str(ROOT / "output/playwright/quality-gate-20260906")))
    if not (root / "run-response.json").exists():
        pytest.skip("Set QUALITY_GATE_REPLAY_DIR to the retained E2E bundle")
    snapshot = json.loads((root / "run-response.json").read_text(encoding="utf-8"))
    artifacts = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in (root / "research-task-artifacts").glob("*.json")
    }
    assert len(artifacts) == 11
    return snapshot, artifacts


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch, tmp_path):
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def guarded(original):
        def connect(sock, address):
            # Windows asyncio creates a loopback socket pair for its wakeup pipe.
            if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
                return original(sock, address)
            pytest.fail("Offline replay attempted an external socket connection")
        return connect

    def blocked(*_args, **_kwargs):
        pytest.fail("Offline replay attempted an external socket connection")

    monkeypatch.setattr(socket.socket, "connect", guarded(original_connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guarded(original_connect_ex))
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))


def replay_config(tmp_path, task_id="supervisor"):
    return {
        "configurable": {
            "runs_dir": str(tmp_path), "model_backend": "litellm",
            "quality_evaluation_enabled": True, "quality_evaluation_model": "if-quality-v1",
            "quality_evaluation_rigor": "balanced", "quality_evaluation_min_sources": 3,
            "quality_evaluation_max_input_chars": 30000, "quality_evaluation_fail_open": True,
            "report_review_enabled": False, "observability_enabled": False,
            "event_log_enabled": True, "sandbox_enabled": False,
        },
        "metadata": {
            "run_id": "offline-quality-replay", "task_id": task_id,
            "runtime_config_frozen": True, "quality_policy_version": "quality-gate-v4",
            "quality_evaluation_epoch": "offline-replay",
        },
    }


def fake_model_transport(monkeypatch, response):
    calls = []

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["model"] == "if-quality-v1"
        return httpx.Response(200, json={
            "id": "offline", "object": "chat.completion", "created": 0,
            "model": "if-quality-v1", "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": "offline-call", "type": "function", "function": {
                        "name": "__insightforge_structured_output", "arguments": json.dumps(response),
                    },
                }],
            }}],
        })

    client = AsyncOpenAI(api_key="offline-only", base_url="http://offline.invalid/v1", max_retries=0,
                         http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    gateway = LiteLLMModelGateway(base_url="http://offline.invalid/v1", api_key="offline-only", client=client)
    monkeypatch.setattr(invocation, "get_model_gateway", lambda _: gateway)
    return client, calls


@pytest.mark.asyncio
async def test_recorded_eleven_handoffs_feed_real_terminal_pipeline(recorded_bundle, monkeypatch, tmp_path):
    snapshot, artifacts = recorded_bundle
    state = copy.deepcopy(snapshot["result"])
    projected = {}
    client, calls = fake_model_transport(monkeypatch, projected)
    assessments = []
    try:
        for saved in state["handoff_assessments"]:
            task_id = saved["tool_call_id"]
            artifact = artifacts[task_id]
            projected.clear()
            # Gate-computed diagnostics are deliberately recomputed, not replayed.
            projected.update({key: saved[key] for key in (
                "accepted", "admission_status", "relevance", "source_quality", "evidence_coverage",
                "groundedness", "missing_information", "unsupported_claims", "follow_up_tasks",
                "requirement_coverage", "caveats", "reason",
            ) if key in saved})
            assessed = await gate.evaluate_subagent_handoff(
                artifact["research_topic"], artifact, replay_config(tmp_path, task_id),
                coverage_contract=artifact["coverage_contract"], requirement_ids=artifact["requirement_ids"],
            )
            assert assessed.evaluator_error is None, (task_id, assessed.evaluator_error)
            assert assessed.admission_status.value == "rejected"
            assert "score_below_dimension_floor" in assessed.hard_rejection_reasons
            assert merge_coverage_ledger({}, task_id=task_id, assessment=assessed,
                                         owned_requirement_ids=artifact["requirement_ids"]) == {}
            assessments.append({"tool_call_id": task_id, **assessed.model_dump(mode="json")})
    finally:
        await client.close()

    assert len(calls) == 11  # No unexpected Judge protocol retries.
    state["handoff_assessments"] = assessments
    state.pop("result", None)
    assert state["research_artifact_refs"] == {}  # Matches the captured terminal input.
    assert state["evidence_registry"] == []
    engine = QueryEngine(replay_config(tmp_path))
    engine.context_store.initialize(None, engine.config)

    async def captured_supervisor(_state):
        return {}  # Research is already complete in the recorded state.

    monkeypatch.setattr(engine, "_run_supervisor", captured_supervisor)
    events = [event async for event in engine._stream_execution(state, "supervisor.supervisor")]
    assert events
    assert engine.status == "failed"
    assert engine.final_state["result"]["error_code"] == "insufficient_evidence"
    assert engine.final_state["result"]["termination_reason"] == "max_turns_drained"
    assert not engine.final_state.get("final_report")
    public = event_publisher_from_config(engine.config).store.read()
    assert any(event.type == "run.failed" for event in public)
    assert not any(event.type.startswith("report.") for event in public)
    assert not list(tmp_path.rglob("final_report.md"))


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_requirement", [False, True])
async def test_supplemental_supported_evidence_recovers_complete_or_partial(monkeypatch, tmp_path, missing_requirement):
    requirements = [{
        "requirement_id": "COV-01-replay", "text": "Verify recycling rate", "kind": "factual",
        "source_message_index": 0, "source_start": 0, "source_end": 21,
    }]
    if missing_requirement:
        requirements.append({**requirements[0], "requirement_id": "COV-02-replay", "text": "Verify cost"})
    contract = {"original_query_sha256": "supplemental", "requirements": requirements}
    evidence = [{
        "evidence_id": f"EV-{i}", "claim": "The measured recycling rate is 90%.",
        "supporting_excerpt": "The measured recycling rate is 90%.",
        "source_url": f"https://source{i}.example/recycling", "source_title": f"Source {i}",
        "security_status": "accepted",
    } for i in range(3)]
    response = {
        "accepted": True, "admission_status": "accepted", "relevance": 5,
        "source_quality": 5, "evidence_coverage": 5, "groundedness": 5,
        "reason": "Supplemental fixed positive Judge response.",
        "requirement_coverage": [{"requirement_id": "COV-01-replay", "status": "supported",
                                  "evidence_ids": [row["evidence_id"] for row in evidence]}],
    }
    client, calls = fake_model_transport(monkeypatch, response)
    engine = QueryEngine(replay_config(tmp_path))
    engine.context_store.initialize(None, engine.config)
    artifact = {
        "schema_version": 2, "research_topic": "Verify recycling rate", "coverage_contract": contract,
        "requirement_ids": ["COV-01-replay"], "evidence_registry": evidence,
        "compressed_research": "The measured recycling rate is 90%. " * 20,
        "metrics": {"sources_read": 3},
    }
    digest = engine.context_store.persist_task_result("positive", artifact)
    state = {
        "messages": [], "coverage_contract": contract, "evidence_registry": [], "notes": [],
        "research_artifact_refs": {"positive": {"sha256": digest}},
        "completion_decision": {"action": "terminate", "reason": "max_turns_drained", "gaps": ["accepted_evidence"]},
    }
    try:
        result = await engine._recover_quality_gate_termination(state)
        assert result["mode"] == "accepted", state.get("handoff_assessments")
        assert len(calls) == 1
        assert state["coverage_ledger"]["COV-01-replay"]["status"] == "supported"
        assert state["completion_decision"]["action"] == ("complete_partial" if missing_requirement else "complete")
        assert len(state["evidence_registry"]) == 3
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_supplemental_judge_unavailable_cannot_admit_recorded_artifact(recorded_bundle, monkeypatch, tmp_path):
    _, artifacts = recorded_bundle
    artifact = next(iter(artifacts.values()))

    async def unavailable(*_args, **_kwargs):
        raise TimeoutError("supplemental offline timeout")

    monkeypatch.setattr(invocation.LiteLLMModelGateway, "complete", unavailable)
    client, _ = fake_model_transport(monkeypatch, {})
    try:
        result = await gate.evaluate_subagent_handoff(
            artifact["research_topic"], artifact, replay_config(tmp_path),
            coverage_contract=artifact["coverage_contract"], requirement_ids=artifact["requirement_ids"],
        )
        assert result.evaluator_error is not None
        assert not result.accepted
        assert "quality_evaluator_unavailable" in result.hard_rejection_reasons
    finally:
        await client.close()
