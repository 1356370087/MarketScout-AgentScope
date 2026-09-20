"""Document reads stay in Gateway and preserve errors instead of quarantining."""

# ruff: noqa: F811 -- imported pytest fixtures

import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_gateway_ledger import host  # noqa: F401
from test_production_resources import config
from test_recovery import create, store  # noqa: F401

from open_deep_research.agentscope_runtime import production_resources as resources
from open_deep_research.agentscope_runtime.recovery import RecoverySession
from open_deep_research.agentscope_runtime.run_config import RunConfig
from open_deep_research.models.credentials_context import current_run_key
from open_deep_research.sandbox.gateway import GatewayRunContext
from open_deep_research.sandbox.wire import GatewayToolRequestV1
from open_deep_research.tools.base import ToolExecutionZone
from open_deep_research.tools.governance import GovernedToolCallResult, ToolError, ToolOutcomeMessage
from open_deep_research.tools.search_documents import search_documents

pytestmark = pytest.mark.asyncio


async def test_sensitive_read_error_is_committed_and_replayed(store):
    state, lease = await create(store, limits={"tool_calls": 1})
    session = RecoverySession(store, lease, state)
    calls = []

    async def fail():
        calls.append(1)
        return GovernedToolCallResult(
            ToolOutcomeMessage("retrieval unavailable", "search_documents", "c"),
            error=ToolError(error_type="network_error", tool_name="search_documents",
                            message="retrieval unavailable"),
        )

    try:
        result = await session.tool(search_documents, "c", {"query": "q"}, fail)
        replay = await session.tool(search_documents, "c", {"query": "q"}, fail)
        assert result.error == replay.error and calls == [1]
        assert session.problem is None
        budget = await store.budget(state.run_id, "owner")
        assert budget["used"]["tool_calls"] == 1
        assert budget["reserved"]["tool_calls"] == 0
    finally:
        await session.close()


async def test_worker_keeps_document_gateway_proxy(store, monkeypatch, tmp_path):
    state, lease = await create(store)
    session = RecoverySession(store, lease, state)
    cfg = config()
    cfg["metadata"] = {"source_selection": {"mode": "documents", "sources": [
        {"type": "document", "id": "doc"}]}}
    proxy = SimpleNamespace(name="search_documents", execution_zone=ToolExecutionZone.GATEWAY)

    async def catalog(*args, **kwargs):
        return [proxy]

    monkeypatch.setattr(resources, "load_gateway_catalog_tools", catalog)
    # This used to cause a credential-less local implementation to replace proxy.
    from open_deep_research.documents import database
    monkeypatch.setattr(database, "document_schema_available", lambda: True)
    try:
        async with resources.production_resources(tmp_path, worker_task_id="task")(
            RunConfig.compile(cfg), cfg, session
        ) as ports:
            tools = await ports.tools_for(SimpleNamespace(task_id="task"))
            assert tools == [proxy]
            assert tools[0].execution_zone not in ports.local_zones
            with pytest.raises(RuntimeError, match="run_key_unavailable"):
                current_run_key()
    finally:
        await session.close()


@pytest.mark.parametrize("permitted", [False, True])
async def test_gateway_document_read_has_run_key_and_durable_receipt(host, monkeypatch, permitted):
    from open_deep_research.documents import database
    from open_deep_research.tools.search_documents import definition
    from security.rbac.dependencies import apply_principal_to_config
    from tests.auth_helpers import research_principal

    gateway, ledger, _ = host
    principal = research_principal("u")
    if permitted:
        principal = replace(principal, permissions=principal.permissions | {"research.tool.document"})
    cfg = apply_principal_to_config({"configurable": {
        "enable_async_research": False, "event_log_enabled": False,
        "search_api": "none", "web_pipeline_mode": "legacy",
    }}, principal)
    cfg["metadata"]["source_selection"] = {"mode": "documents", "sources": [
        {"type": "document", "id": "doc"}]}
    context = GatewayRunContext(cfg, ledger.recovery.lease.fence, time.time() + 300,
                                api_keys={"LITELLM_RUN_KEY": "run-only-fixture"})
    calls = []

    async def retrieve(**kwargs):
        assert current_run_key() == "run-only-fixture"
        assert kwargs["owner_id"] == "u" and kwargs["document_ids"] == ["doc"]
        assert kwargs["run_id"] == "r"
        calls.append(kwargs)
        return [{"document_id": "doc", "chunk_id": "chunk", "filename": "source.md",
                 "text": "Supported finding", "locator": {"page": 1}, "score": 0.05,
                 "source_uri": "/documents/doc?chunk=chunk"}]

    monkeypatch.setattr(database, "document_schema_available", lambda: True)
    monkeypatch.setattr(definition, "document_schema_available", lambda: True)
    monkeypatch.setattr(definition, "search_document_chunks", retrieve)
    request = GatewayToolRequestV1(run_id="r", task_id="t", role="researcher",
        stage="researching", logical_operation_id="document-read", tool_call_id="c",
        tool_name="search_documents", execution_zone="gateway", arguments={"query": "finding"})
    result = await gateway.invoke_tool(request, context)
    if not permitted:
        assert result.status == "failed" and result.error["error_type"] == "permission_denied"
        assert not calls
        with pytest.raises(RuntimeError, match="run_key_unavailable"):
            current_run_key()
        return
    assert result.status == "completed", result
    assert json.loads(result.output)["evidence"][0]["source_type"] == "local_document"
    assert await gateway.invoke_tool(request, context) == result
    assert len(calls) == 1
    row = await ledger.recovery.store.operation_record(ledger.recovery.lease,
                                                       "gateway:tool:document-read")
    assert row["state"] == "committed" and row["replay_safe"]
    with pytest.raises(RuntimeError, match="run_key_unavailable"):
        current_run_key()


async def test_document_profile_does_not_allow_other_sensitive_tools():
    from open_deep_research.configuration import Configuration
    from open_deep_research.sandbox.schema import resolve_profile, tool_policy_decision

    _, _, profile = resolve_profile(Configuration())
    assert tool_policy_decision(profile, tool_name="search_documents", effect="sensitive_read") == "allow"
    assert tool_policy_decision(profile, tool_name="read_file", effect="sensitive_read") == "deny"
    assert tool_policy_decision(profile, tool_name="write_file", effect="local_write") == "deny"
    profile.tools.deny_tools = ["search_*"]
    assert tool_policy_decision(profile, tool_name="search_documents", effect="sensitive_read") == "deny"
