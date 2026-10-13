"""SDK, Gateway and SQL regression coverage for model accounting boundaries."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import httpx2
import pytest
from agentscope.credential import OpenAICredential
from agentscope.message import UserMsg
from pydantic import BaseModel
from test_recovery import create, store
from test_gateway_ledger import host
from test_native_observability import empty_response
from test_server_search_models import anthropic_events, openai_events, sse

from open_deep_research.agentscope_runtime.gateway import LiteLLMChatModel
from open_deep_research.agentscope_runtime.model_policy import ModelCallPolicy
from open_deep_research.agentscope_runtime.recovery import RecoverySession
from open_deep_research.agentscope_runtime.sandbox_provider import NativeGatewayProvider
from open_deep_research.agentscope_runtime.search_models import ServerSearchModel
from open_deep_research.agentscope_runtime.usage_projection import project_usage
from open_deep_research.sandbox.wire import GatewayModelRequestV2, ServerSearchRequest


@pytest.mark.asyncio
async def test_native_run_is_in_usage_analytics(store, tmp_path, monkeypatch):
    from open_deep_research.api import usage
    from open_deep_research.configuration import Configuration

    state, lease = await create(store)
    await store.begin_operation(lease, "gateway:model:one", "gateway:model", {},
                                reserve={"model_calls": 1})
    tokens = {"input_tokens": 12, "output_tokens": 5}
    await store.commit_operation(lease, "gateway:model:one", {
        "status": "completed", "outcome": {"usage": tokens}},
        actual={"model_calls": 1, **tokens, "cost_micro_usd": 2000})
    detail = await project_usage(store, state.run_id, "owner", empty_response())
    assert detail["totals"]["reported"]["total_tokens"] == 17
    cfg = Configuration(trace_store_path=str(tmp_path / "old-traces.db"),
                        runs_dir=str(tmp_path / "runs"))
    monkeypatch.setattr(usage.Configuration, "from_runnable_config", lambda _: cfg)
    history = usage._build_usage_analytics_response(
        range_name="30d", status=None, provider=None, model=None, query=None,
        timezone_name="Asia/Shanghai", timezone_info=ZoneInfo("Asia/Shanghai"),
        limit=50, offset=0, user_id="owner",
        gateway_spend_by_run={state.run_id: 2000},
        native_reports=await usage._native_usage_reports(store, "owner"))
    assert history["summary"]["reported"]["total_tokens"] == 17, history["summary"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_server_search_carries_reconciliation_tags(provider):
    requests = []

    def transport(request):
        requests.append(json.loads(request.content))
        http = httpx if provider == "openai" else httpx2
        events = openai_events("search") if provider == "openai" else anthropic_events("search")
        return http.Response(200, headers={"content-type": "text/event-stream"}, text=sse(events))

    gateway = NativeGatewayProvider(api_key="fixture", base_url="https://fixture.invalid/v1",
        transport=httpx.MockTransport(transport), anthropic_transport=httpx2.MockTransport(transport))
    request = GatewayModelRequestV2(run_id="run-review", task_id="task", stage="researching",
        role=f"{provider}_search", logical_operation_id="operation-review", model="search",
        messages=[{"role": "user", "content": "query"}], max_output_tokens=100,
        server_search=ServerSearchRequest(provider=provider, query="query"))
    try:
        outcome = await gateway.complete(request)
        assert outcome.usage["input_tokens"] == 7
    finally:
        await gateway.aclose()
    tags = requests[0]["litellm_metadata"]["tags"]
    assert "metadata" not in requests[0]  # Do not send Proxy tags as provider metadata.
    assert {"run:run-review", "operation:operation-review"}.issubset(tags), requests[0]


@pytest.mark.asyncio
async def test_anthropic_search_includes_cache_read_and_creation_input():
    events = anthropic_events("search")
    events[0]["message"]["usage"].update(cache_read_input_tokens=11,
                                        cache_creation_input_tokens=13)

    def transport(request):
        return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                               text=sse(events))

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(transport)) as client:
        model = ServerSearchModel(provider="anthropic", model="anthropic:search", api_key="fixture",
            base_url="https://fixture.invalid", http_client=client)
        response = await model.search_web("query")
    assert response.usage.input_tokens == 31, response.metadata["raw_usage"]
    assert response.metadata["raw_usage"]["cache_creation_input_tokens"] == 13


@pytest.mark.asyncio
@pytest.mark.parametrize("usage_present", [True, False])
async def test_run_embedding_enters_native_ledger_and_replays(host, monkeypatch, usage_present):
    import time
    from openai import AsyncOpenAI
    from open_deep_research.documents import embeddings
    from open_deep_research.documents.settings import DocumentSettings
    from open_deep_research.sandbox.gateway import GatewayRunContext, GatewayRuntime
    from open_deep_research.sandbox.wire import GatewayToolRequestV1

    gateway, ledger, _ = host
    store, lease = ledger.recovery.store, ledger.recovery.lease
    ledger.catalog["if-embedding-v1"] = {"input_cost_per_token": 0.000001, "output_cost_per_token": 0}
    await store.save(lease, ledger.recovery.snapshot, limits={"input_tokens": 1000, "cost_micro_usd": 1000})
    calls = []

    def transport(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"object": "list", "model": "if-embedding-v1",
            "data": [{"index": 0, "object": "embedding", "embedding": [0.1, 0.2]}],
            "usage": {"prompt_tokens": 17, "total_tokens": 17} if usage_present else None})

    monkeypatch.setenv("LITELLM_BASE_URL", "https://fixture.invalid/v1")
    monkeypatch.setattr(embeddings, "get_prometheus_metrics", lambda *args: None)

    async def tool(request, context):
        return await embeddings.embed_texts(["query"], DocumentSettings(embedding_dimensions=2), operation="query")

    monkeypatch.setattr(gateway, "_invoke_tool", tool)
    request = GatewayToolRequestV1(run_id="r", task_id="t", role="researcher", stage="researching",
        logical_operation_id="retrieval", tool_call_id="c", tool_name="search_documents",
        execution_zone="gateway", arguments={"query": "query"})
    context = GatewayRunContext({}, lease.fence, time.time() + 300, api_keys={"LITELLM_RUN_KEY": "fixture"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = AsyncOpenAI(api_key="fixture", base_url="https://fixture.invalid/v1", http_client=http)
        monkeypatch.setattr(embeddings, "_get_embedding_client", lambda *args: client)
        results = await asyncio.gather(*(GatewayRuntime.invoke_tool.__wrapped__(gateway, request, context) for _ in range(2)))
        assert results == [[[0.1, 0.2]], [[0.1, 0.2]]]
        assert await GatewayRuntime.invoke_tool.__wrapped__(gateway, request, context) == [[0.1, 0.2]]
    assert len(calls) == 1
    assert "run:r" in calls[0]["metadata"]["tags"]
    budget = await store.budget("r", "u")
    assert budget["used"]["model_calls"] == 1
    if usage_present:
        assert budget["used"] == {"model_calls": 1, "input_tokens": 17, "output_tokens": 0, "cost_micro_usd": 17}
    else:
        assert budget["used"]["input_tokens"] > 17 and budget["used"]["cost_micro_usd"] > 0
    assert not any(budget["reserved"].values())
    report = await project_usage(store, "r", "u", empty_response())
    assert report["totals"]["reported"]["input_tokens"] == (17 if usage_present else 0)
    assert report["accounting_status"] == ("complete" if usage_present else "partial")
    assert report["breakdowns"]["by_agent_role"][0]["key"] == "embedding"
    assert embeddings.run_embedding_accounting.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_invalid_structured_output_keeps_measured_usage(store, stream):
    from agentscope.exception import StructuredOutputError
    class Answer(BaseModel):
        value: int

    def transport(request):
        data = {"id": "reply", "object": "chat.completion",
            "created": 1, "model": "fixture", "choices": [{"index": 0,
                "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": "call", "type": "function", "function": {
                    "name": "generate_structured_output", "arguments": '{"value":"invalid"}'}}]}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}}
        if stream:
            message = data["choices"][0].pop("message")
            message["tool_calls"][0]["index"] = 0
            data["choices"][0]["delta"] = message
            data["object"] = "chat.completion.chunk"
            measured = data.pop("usage")
            tail = {**data, "choices": [], "usage": measured}
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                text=f"data: {json.dumps(data)}\n\ndata: {json.dumps(tail)}\n\ndata: [DONE]\n\n")
        return httpx.Response(200, json=data)

    state, lease = await create(store)
    session = RecoverySession(store, lease, state)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        model = LiteLLMChatModel(model="fixture", stream=stream, max_retries=0,
            credential=OpenAICredential(api_key="fixture", base_url="https://fixture.invalid/v1"),
            client_kwargs={"http_client": client, "max_retries": 0})

        async def handler(current_model, messages):
            return await current_model.generate_structured_output(messages, Answer)

        async def call():
            return await ModelCallPolicy([model], attempts=1).invoke(handler,
                {"messages": [UserMsg("u", "q")]}, {})

        with pytest.raises(StructuredOutputError):
            with session.scope("research", 0):
                await session.model("researcher", [UserMsg("u", "q")], call,
                                    account_attempts=True, max_tokens=100)
        row = await store.operation_record(lease, "research:0:pipeline:model:researcher:0:attempt:0")
        assert row is not None
        assert row["result"]["observed_usage"] == {"input_tokens": 12, "output_tokens": 5}, row["result"]
        assert row["actual"] == {"model_calls": 1, "input_tokens": 12, "output_tokens": 5}


@pytest.mark.asyncio
async def test_cancelled_stream_usage_is_not_marked_complete(store):
    from agentscope.credential import AnthropicCredential
    from open_deep_research.agentscope_runtime.provider_models import GovernedAnthropicChatModel

    state, lease = await create(store)
    session = RecoverySession(store, lease, state)

    class InterruptedBody(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield sse(anthropic_events("fixture")[:3]).encode()
            raise asyncio.CancelledError()

    def transport(request):
        return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                               stream=InterruptedBody())

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(transport)) as client:
        model = GovernedAnthropicChatModel(credential=AnthropicCredential(api_key="fixture"),
            model="fixture", stream=True, max_retries=0,
            client_kwargs={"max_retries": 0, "http_client": client})

        async def handler(current_model, messages):
            return await current_model(messages=messages)

        async def call():
            return await ModelCallPolicy([model], attempts=1).invoke(handler,
                {"messages": [UserMsg("u", "q")]}, {})

        with pytest.raises(asyncio.CancelledError):
            with session.scope("research", 0):
                await session.model("researcher", [], call, account_attempts=True, max_tokens=100)
    row = await store.operation_record(lease, "research:0:pipeline:model:researcher:0:attempt:0")
    assert row["result"]["usage_status"] != "reported", row["result"]
    assert row["result"]["status"] == "failed" and row["result"]["uncertain"]
    assert row["actual"]["output_tokens"] == 100


@pytest.mark.asyncio
async def test_spend_pagination_deduplicates_overlapping_pages():
    from open_deep_research.models.credentials import RunKeySettings
    from open_deep_research.models.spend import LiteLLMSpendClient

    rows = [{"request_id": "one", "request_tags": ["run:r"], "total_tokens": 10, "spend": 0.01},
            {"request_id": "two", "request_tags": ["run:r"], "total_tokens": 20, "spend": 0.02}]
    def transport(request):
        assert request.url.path == "/spend/logs/v2"
        assert request.url.params["page_size"] == "2"
        assert "start_date" in request.url.params and "end_date" in request.url.params
        return httpx.Response(200, json={"data": rows, "total_pages": 2})
    settings = RunKeySettings("https://fixture.invalid/v1", "fixture", "team", b"0" * 32, 1, 10)
    async with httpx.AsyncClient(base_url="https://fixture.invalid", transport=httpx.MockTransport(transport)) as client:
        spend = LiteLLMSpendClient(settings, client=client)
        result = await spend.spend_logs(size=2, max_pages=4)
    assert len(result) == 2, [row["request_id"] for row in result]


@pytest.mark.asyncio
@pytest.mark.parametrize("structured", [False, True])
async def test_gateway_reservation_covers_tool_schema(host, structured):
    import time
    from open_deep_research.sandbox.gateway import GatewayRunContext
    from open_deep_research.sandbox.wire import GatewayModelOutcomeV2

    runtime, ledger, _ = host
    store, lease = ledger.recovery.store, ledger.recovery.lease
    await store.save(lease, ledger.recovery.snapshot, limits={"input_tokens": 100})
    calls = []

    class Provider:
        async def complete(self, request):
            calls.append(request)
            return GatewayModelOutcomeV2(logical_operation_id=request.logical_operation_id,
                requested_model=request.model, status="completed", finish_reason="stop",
                message={"role": "assistant", "content": "ok"},
                usage={"input_tokens": 800, "output_tokens": 1})

    runtime.model_gateways["r"] = Provider()
    schema = {"type": "object", "description": "words " * 800, "properties": {}}
    request = GatewayModelRequestV2(run_id="r", task_id="task", role="researcher",
        stage="researching", model="fixture", logical_operation_id="schema",
        messages=[{"role": "user", "content": "q"}], max_output_tokens=10,
        tools=[] if structured else [{"type": "function", "function": {"name": "tool", "parameters": schema}}],
        structured_schema=schema if structured else None)
    context = GatewayRunContext({}, lease.fence, time.time() + 300,
                                api_keys={"LITELLM_RUN_KEY": "fixture"})
    result = await runtime.invoke_model_operation_v2(request, context)
    assert not calls
    assert result.error_code == "budget_exhausted:input_tokens"
    assert await store.operation_record(lease, "gateway:model:schema") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("dimension", ["input_tokens", "cost_micro_usd", "model_calls"])
async def test_embedding_budget_rejection_precedes_provider_io(host, dimension):
    from open_deep_research.agentscope_runtime.embedding_accounting import GatewayEmbeddingAccounting
    from open_deep_research.budgets import BudgetExhausted

    gateway, ledger, _ = host
    ledger.catalog["embedding"] = {"input_cost_per_token": 0.000001, "output_cost_per_token": 0}
    await ledger.recovery.store.save(ledger.recovery.lease, ledger.recovery.snapshot, limits={dimension: 0})
    request = SimpleNamespace(run_id="r", task_id="t", stage="researching", logical_operation_id="retrieval", tool_call_id="tool")
    accounting = GatewayEmbeddingAccounting(gateway, request, SimpleNamespace(fence_token=ledger.recovery.lease.fence))
    call = AsyncMock()
    with pytest.raises(BudgetExhausted) as error:
        await accounting.invoke("embedding", ["query"], call)
    assert error.value.dimension.value == dimension
    call.assert_not_awaited()
    budget = await ledger.recovery.store.budget("r", "u")
    assert not budget["used"] and not budget["reserved"]


@pytest.mark.asyncio
async def test_embedding_timeout_keeps_charge_and_late_bill_never_repeats_call(host):
    from sqlalchemy import select
    from open_deep_research.agentscope_runtime.embedding_accounting import GatewayEmbeddingAccounting
    from open_deep_research.agentscope_runtime.recovery_store import UnknownOperation
    from open_deep_research.agentscope_runtime.spend_reconciliation import reconcile_spend

    gateway, ledger, _ = host
    ledger.catalog["embedding"] = {"input_cost_per_token": 0.000001, "output_cost_per_token": 0}
    await ledger.recovery.store.save(ledger.recovery.lease, ledger.recovery.snapshot, limits={"cost_micro_usd": 1000})
    request = SimpleNamespace(run_id="r", task_id="t", stage="researching", logical_operation_id="retrieval", tool_call_id="tool")
    accounting = GatewayEmbeddingAccounting(gateway, request, SimpleNamespace(fence_token=ledger.recovery.lease.fence))
    call = AsyncMock(side_effect=TimeoutError())
    with pytest.raises(TimeoutError):
        await accounting.invoke("embedding", ["query"], call)
    store = ledger.recovery.store
    budget = await store.budget("r", "u")
    assert budget["used"]["model_calls"] == 1 and budget["used"]["input_tokens"] > 0
    async with store.engine.connect() as connection:
        row = (await connection.execute(select(store.ops))).mappings().one()
    assert row["result"]["status"] == "uncertain"
    bill = {"request_id": "embedding-bill", "request_tags": ["run:r", f"operation:{row['key'].removeprefix('gateway:model:')}"],
            "prompt_tokens": 17, "completion_tokens": 0, "spend": 0.000017}
    for _ in range(2):
        assert (await reconcile_spend(store, ledger.recovery.lease, [bill]))["corrected"] == [row["key"]]
    assert (await store.budget("r", "u"))["used"]["input_tokens"] == 17
    with pytest.raises(UnknownOperation):
        await accounting.invoke("embedding", ["query"], call)
    assert call.await_count == 1


@pytest.mark.asyncio
async def test_usage_http_merges_native_history_and_isolates_owner(store, tmp_path, monkeypatch):
    import time
    from fastapi import FastAPI
    from open_deep_research.api import usage
    from open_deep_research.agentscope_runtime.research_pipeline import ResearchSnapshot
    from open_deep_research.configuration import Configuration
    from open_deep_research.observability.tracing import SQLiteTraceStore
    from security.rbac import get_current_principal
    from tests.auth_helpers import research_principal

    now = time.time()
    for run_id, owner, age, title in (("native", "owner", 0, "Native review"),
                                     ("old", "owner", 100, "Old native"),
                                     ("foreign", "other", 0, "Private title")):
        state = ResearchSnapshot(run_id=run_id, config_fingerprint="f", status="completed",
                                  application={"created_at": now - age * 86400, "title": title})
        await store.create_run(owner, state)
        lease = await store.acquire(run_id, owner)
        for key, provider in (("alias-a", "openai"), ("alias-b", "anthropic")):
            await store.begin_operation(lease, key, "gateway:model", {}, reserve={"model_calls": 1},
                observation={"model": key, "agent_role": "researcher"})
            await store.commit_operation(lease, key, {"status": "completed", "outcome": {
                "provider": provider, "requested_model": key, "usage": {"input_tokens": 2, "output_tokens": 1}}},
                actual={"model_calls": 1, "input_tokens": 2, "output_tokens": 1, "cost_micro_usd": 3})
        await store.release(lease)
    cfg = Configuration(trace_store_path=str(tmp_path / "traces.db"), runs_dir=str(tmp_path / "runs"), trace_retention_days=0)
    monkeypatch.setattr(usage.Configuration, "from_runnable_config", lambda _: cfg)
    traces = SQLiteTraceStore(cfg.trace_store_path)
    traces.start_run("native", "owner", {"title": "Obsolete trace"})
    traces.start_run("legacy", "owner", {"title": "Legacy"})
    traces.start_run("foreign-trace", "other", {"title": "Private trace"})
    monkeypatch.setattr(usage, "_fetch_gateway_run_spend", AsyncMock(return_value={"native": 1000}))
    app = FastAPI()
    app.state.native_research_service = SimpleNamespace(store=store)
    app.include_router(usage.router)
    app.dependency_overrides[get_current_principal] = lambda: research_principal("owner")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://fixture") as client:
        result = (await client.get("/usage/analytics", params={"range": "retained", "limit": 1})).json()
        assert result["summary"]["run_count"] == 3
        assert result["summary"]["reported"]["total_tokens"] == 12
        assert "Private" not in json.dumps(result)
        pages = list(result["runs"])
        while result["next_cursor"]:
            result = (await client.get("/usage/analytics", params={"range": "retained", "limit": 1, "cursor": result["next_cursor"]})).json()
            pages.extend(result["runs"])
        assert {row["run_id"] for row in pages} == {"native", "old", "legacy"}
        filtered = (await client.get("/usage/analytics", params={
            "range": "7d", "status": "completed", "provider": "openai", "model": "alias-a", "query": "Native review"})).json()
        assert filtered["summary"]["run_count"] == 1
        assert filtered["summary"]["reported"]["total_tokens"] == 3
        assert filtered["summary"]["estimated_cost_micro_usd"] == 3  # Never overlay the whole run's 1000 on a subset.
        assert filtered["runs"][0]["title"] == "Native review"
        assert filtered["distributions"]["provider"][0]["key"] == "openai"
