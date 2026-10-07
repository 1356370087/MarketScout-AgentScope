"""Public-boundary and algorithm regressions for knowledge plan A."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from agentscope.message import TextBlock

from open_deep_research.documents.contracts import SourceSelection
from open_deep_research.events.task_activity import sanitize_task_activity_payload
from open_deep_research.knowledge.evaluation import ndcg_at_k
from open_deep_research.knowledge.execution import SearchExecution
from open_deep_research.knowledge.rerank import rerank_segments
from open_deep_research.knowledge.sync import SyncError, WebAdapter
from open_deep_research.security.inputs import validate_http_metadata


def test_ndcg_counts_each_rank_once_when_labels_share_a_chunk():
    assert ndcg_at_k(["alpha", "beta"], [{"text": "alpha beta"}], 12) == 1.0
    assert 0 <= ndcg_at_k(["alpha", "beta"], [{"text": "noise"}, {"text": "alpha beta"}], 12) <= 1


def test_client_cannot_supply_the_trusted_knowledge_manifest():
    for key in ("knowledge_manifest", "selected_source_snapshots"):
        with pytest.raises(ValueError, match="Protected runtime metadata"):
            validate_http_metadata({key: {"documents": []}})


def test_run_embedding_never_borrows_service_key(monkeypatch):
    monkeypatch.setenv("LITELLM_SERVICE_KEY", "fixture-service-only")
    with pytest.raises(RuntimeError):
        SearchExecution(scope="run").embedding_key()
    assert SearchExecution().embedding_key() == "fixture-service-only"


@pytest.mark.asyncio
async def test_run_rerank_uses_registered_native_role_not_service_transport(monkeypatch):
    standalone = AsyncMock(side_effect=AssertionError("service model must not run"))
    monkeypatch.setattr("open_deep_research.agentscope_runtime.service_models.service_text", standalone)
    policy = SimpleNamespace(invoke=AsyncMock(return_value=SimpleNamespace(content=[TextBlock(text='{"scores":[{"id":"segment","score":3}]}')])))
    models = SimpleNamespace(policy_middleware=Mock(return_value=SimpleNamespace(policy=policy)))
    scores = await rerank_segments("query", [{"id": "segment", "text": "support"}], execution=SearchExecution(scope="run", models=models))
    assert scores["segment"][0] == 3
    models.policy_middleware.assert_called_once_with("knowledge_rerank")
    standalone.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_rejects_unbound_authority_before_any_request():
    send = Mock(side_effect=AssertionError("must not send"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        with pytest.raises(SyncError, match="authorization_required"):
            await WebAdapter()._get(client, "https://public.test", {})
    send.assert_not_called()


def test_local_activity_preserves_exact_version_without_result_body():
    payload = sanitize_task_activity_payload("source.discovered", {
        "url": "/documents/doc-1?chunk=segment-2", "title": "Pricing", "source_type": "local_document",
        "document_id": "doc-1", "chunk_id": "segment-2", "generation_id": "generation-3", "raw_content": "private body",
    })
    assert payload["generation_id"] == "generation-3"
    assert payload["chunk_id"] == "segment-2" and "raw_content" not in payload


def test_historical_source_options_need_a_calendar_cutoff():
    with pytest.raises(ValueError, match="as_of_requires_date"):
        SourceSelection.model_validate({"mode": "documents", "sources": [{"type": "knowledge_base", "id": "base"}], "retrieval": {"version_mode": "as_of"}})


def test_long_source_excerpt_preserves_a_verifiable_quote():
    from open_deep_research.knowledge.evidence_projection import source_excerpt

    text = "Introduction. " * 400 + "Annual subscription USD 1000" + " Appendix." * 400
    excerpt = source_excerpt(text, 1600, "subscription")
    assert len(excerpt) == 1600 and excerpt in text
    assert "Annual subscription USD 1000" in excerpt


def test_v14_restore_keeps_its_fingerprint_and_has_no_implicit_knowledge_model(monkeypatch):
    from open_deep_research.agentscope_runtime.run_config import RunConfig
    from open_deep_research.configuration import run_config_fingerprint

    snapshot = RunConfig.compile().snapshot()
    contract = snapshot["contract"]
    contract["metadata"]["run_config_schema_version"] = 14
    for name in ("knowledge_rerank_model", "knowledge_answer_model"):
        contract["configurable"].pop(name)
    contract["metadata"]["run_config_fingerprint"] = run_config_fingerprint(contract)
    monkeypatch.setenv("KNOWLEDGE_RERANK_MODEL", "new-deployment-reranker")
    monkeypatch.setenv("KNOWLEDGE_ANSWER_MODEL", "new-deployment-answer")
    restored = RunConfig.restore(snapshot)
    assert restored.snapshot()["contract"]["metadata"]["run_config_fingerprint"] == contract["metadata"]["run_config_fingerprint"]
    assert restored.get("knowledge_rerank_model") is None
    assert restored.get("knowledge_answer_model") is None


@pytest.mark.asyncio
async def test_host_keeps_knowledge_reads_at_the_gateway_boundary(monkeypatch):
    from open_deep_research.agentscope_runtime.native_host import (
        _host_local_zones,
        _host_tools_for,
    )
    from open_deep_research.tools.base import ToolExecutionZone

    monkeypatch.setattr("open_deep_research.documents.database.document_schema_available", lambda: True)
    monkeypatch.setattr("open_deep_research.tools.search_documents.definition.document_schema_available", lambda: True)
    monkeypatch.setattr("open_deep_research.knowledge.research_assets.document_schema_available", lambda: True)
    config = {"metadata": {"source_selection": {"mode": "documents", "sources": [{"type": "document", "id": "doc"}]},
                           "knowledge_manifest": {"documents": [{"document_id": "doc", "generation_id": "generation"}]}}}
    tools = await _host_tools_for("researcher", config)
    knowledge = [tool for tool in tools if tool.name in {"search_documents", "knowledge_facts", "knowledge_wiki"}]
    assert len(knowledge) == 3
    assert all(tool.execution_zone is ToolExecutionZone.GATEWAY and tool.execution_zone not in _host_local_zones() for tool in knowledge)
