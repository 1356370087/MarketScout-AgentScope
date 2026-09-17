"""Acceptance of native M8 ports, scoped identity and service model transport."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from agentscope.app.access import ResourceKind
from agentscope.message import UserMsg
from pydantic import ValidationError

from open_deep_research.agentscope_runtime import knowledge, service_models
from open_deep_research.agentscope_runtime.memory import ResearchMemory
from open_deep_research.agentscope_runtime.research_pipeline import ResearchSnapshot
from open_deep_research.configuration import Configuration
from open_deep_research.knowledge.sync import WebAdapter
from open_deep_research.memory.policy import (
    MemoryCandidateModel,
    MemoryExtractionResult,
)
from open_deep_research.memory.store import NoopMemoryStore

pytestmark = pytest.mark.asyncio


async def test_native_service_model_calls_openai_compatible_transport(monkeypatch):
    original = service_models.OpenAIChatModel
    instances = []

    def factory(**kwargs):
        def respond(request):
            body = json.loads(request.content)
            assert (
                body["model"] == "fixture"
                and body["messages"][1]["content"][0]["text"] == "question"
            )
            assert request.headers["authorization"] == "Bearer fixture-key"
            return httpx.Response(
                200,
                json={
                    "id": "c1",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "fixture",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": '{"answer":"ok"}',
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 2,
                        "total_tokens": 5,
                    },
                },
            )

        kwargs["client_kwargs"]["http_client"] = httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        )
        model = original(**kwargs)
        instances.append(model)
        return model

    monkeypatch.setattr(service_models, "OpenAIChatModel", factory)
    assert (
        await service_models.service_text(
            model="fixture",
            api_key="fixture-key",
            base_url="https://example.test/v1",
            system="system",
            prompt="question",
        )
        == '{"answer":"ok"}'
    )
    assert instances[0].client.is_closed()


async def test_scope_bound_search_and_native_operation_catalog(monkeypatch):
    authorize = AsyncMock()
    port = knowledge.KnowledgeApplication("trusted-user", authorize)
    search = AsyncMock(return_value={"results": []})
    monkeypatch.setattr(knowledge, "unified_search", search)
    await port.search(
        knowledge.SearchInput(query="q", version_mode="as_of", as_of_valid="2025-01-01")
    )
    assert search.await_args.args[0].owner_id == "trusted-user"
    with pytest.raises(ValidationError):
        knowledge.SearchInput(query="q", owner_id="other-user")
    tools = port.operation_tools(knowledge.OPERATIONS)
    assert len(tools) == len(knowledge.OPERATIONS)
    for tool in tools:
        assert "actor_id" not in tool.input_schema.model_fields
        assert "owner_id" not in tool.input_schema.model_fields
        assert "authorize_url" not in tool.input_schema.model_fields
    with pytest.raises(PermissionError, match="egress"):
        await port.execute("sync_run", source_id="source")


async def test_resource_policy_rechecks_revocation(monkeypatch):
    caps = AsyncMock(return_value=frozenset({knowledge.authz.CAP_VIEW}))
    monkeypatch.setattr(knowledge.authz, "kb_capabilities", caps)
    policy = knowledge.KnowledgeAccessPolicy({"native": ("domain", "owner")})
    assert (
        len(await policy.list_accessible("viewer", ResourceKind.KNOWLEDGE_BASE, None))
        == 1
    )
    caps.return_value = frozenset()
    assert (
        await policy.list_accessible("viewer", ResourceKind.KNOWLEDGE_BASE, None) == []
    )
    assert not await policy.can_edit(
        "owner", ResourceKind.KNOWLEDGE_BASE, "owner", "native", None
    )


async def test_batch_port_checks_every_target_before_mutation(monkeypatch):
    from open_deep_research.knowledge import batches

    port = knowledge.KnowledgeApplication("actor", AsyncMock())
    port._document_capability = AsyncMock(
        side_effect=[None, PermissionError("revoked")]
    )
    create = AsyncMock()
    monkeypatch.setattr(batches, "create_batch", create)
    with pytest.raises(PermissionError, match="revoked"):
        await port.execute(
            "batch_create", operation="trash", document_ids=["allowed", "revoked"]
        )
    assert port._document_capability.await_count == 2
    create.assert_not_awaited()


async def test_sync_redirect_is_authorized_before_second_request():
    visited = []

    async def authorize(url):
        visited.append(url)
        if "internal" in url:
            raise PermissionError("denied")

    def respond(request):
        assert str(request.url) == "https://public.test/start"
        return httpx.Response(
            302, headers={"location": "https://internal.test/private"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(PermissionError):
            await WebAdapter(authorize)._get(client, "https://public.test/start", {})
    assert len(visited) == 2


def config(tmp_path):
    cfg = Configuration(
        enable_memory=True,
        memory_auto_write=True,
        memory_write_after_report=True,
        memory_project_id="project",
        memory_app_id="app",
        memory_advanced_enabled=False,
        runs_dir=str(tmp_path),
    )
    return {"configurable": cfg.model_dump(), "metadata": {"user_id": "trusted"}}


async def test_memory_native_extraction_scope_no_report_leak_and_noop(tmp_path):
    source = config(tmp_path)
    state = ResearchSnapshot(
        run_id="r",
        config_fingerprint="f",
        messages=[UserMsg("user", "偏好简洁报告")],
        final_report="not extraction input",
    )
    models = SimpleNamespace(
        structured=AsyncMock(
            return_value=MemoryExtractionResult(
                candidates=[
                    MemoryCandidateModel(
                        category="user_research_preference",
                        content="偏好简洁报告",
                        confidence=0.99,
                        reason="user",
                    )
                ]
            )
        )
    )
    store = SimpleNamespace(
        search=AsyncMock(
            return_value=[
                {
                    "memory": "有效偏好",
                    "metadata": {
                        "app_id": "app",
                        "project_id": "project",
                        "user_id": "trusted",
                    },
                },
                {
                    "memory": "其他用户秘密",
                    "metadata": {
                        "app_id": "app",
                        "project_id": "project",
                        "user_id": "other",
                    },
                },
            ]
        ),
        add=AsyncMock(return_value="id"),
    )
    port = ResearchMemory("trusted", models, store_factory=lambda _: store)
    recalled = await port.recall(state, source)
    assert "有效偏好" in recalled and "秘密" not in recalled
    await port.write(state, source)
    assert store.add.await_args.args[1] == "trusted"
    assert store.add.await_args.kwargs["infer"] is False
    assert "not extraction input" not in models.structured.await_args.args[1]
    with pytest.raises(PermissionError):
        await port.recall(state, {**source, "metadata": {"user_id": "other"}})
    noop = ResearchMemory("trusted", models, store_factory=lambda _: NoopMemoryStore())
    assert await noop.recall(state, source) == ""
    await noop.write(state, source)
