"""Physical Gateway charges and stage replay use one SQL budget authority."""

import base64
import time

import httpx
import pytest
import pytest_asyncio
from agentscope.message import TextBlock, UserMsg
from agentscope.model import ChatResponse
from fastapi import FastAPI

from open_deep_research.agentscope_runtime.gateway_ledger import (
    SQLGatewayLedger,
    build_gateway_ledger_router,
)
from open_deep_research.agentscope_runtime.recovery import RecoverySession
from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
from open_deep_research.agentscope_runtime.research_pipeline import ResearchSnapshot
from open_deep_research.configuration import Configuration
from open_deep_research.sandbox.gateway import GatewayRunContext, GatewayRuntime
from open_deep_research.sandbox.internal_api import (
    BudgetReserveRequest,
    OperationGetRequest,
    OperationTransitionRequest,
    ToolBudgetReserveRequest,
    ToolBudgetSettleRequest,
)
from open_deep_research.sandbox.wire import (
    GatewayModelOutcomeV2,
    GatewayModelRequestV2,
    GatewayToolOutcomeV1,
)

pytestmark = pytest.mark.asyncio


async def test_web_reservation_carries_host_source_contract(host):
    gateway, ledger, _ = host
    contract = {"requirements": [{"text": "仅使用 PostgreSQL 官方文档"}]}
    ledger.recovery.snapshot.coverage_contract = contract
    reserved = await _tool_reserve(
        gateway, ledger, "official-search", tool_name="web_research", idempotent=True
    )
    assert reserved["coverage_contract"] == contract


@pytest.mark.parametrize("reservation_committed", [False, True])
@pytest.mark.parametrize("failure", ["http500", "cancel"])
async def test_predispatch_authority_failure_does_not_fabricate_unknown_call(host, reservation_committed, failure):
    import asyncio
    gateway, ledger, _ = host
    post = gateway.internal.post
    requests = []

    async def disconnect(path, request):
        requests.append((path, request.service_nonce))
        if path.endswith("/budgets/reserve"):
            if reservation_committed:
                await post(path, request)
            if failure == "cancel":
                raise asyncio.CancelledError
            raise httpx.HTTPStatusError("authority disconnected", request=httpx.Request("POST", "http://host"),
                                       response=httpx.Response(500))
        return await post(path, request)

    gateway.internal.post = disconnect
    class Provider:
        async def complete(self, request):
            pytest.fail("No physical call may occur after a failed reservation")
    gateway.model_gateways["r"] = Provider()
    request = GatewayModelRequestV2(run_id="r", task_id="t", role="web_evidence", stage="researching",
        logical_operation_id="unreserved", model="m", messages=[{"role": "user", "content": "q"}])
    context = GatewayRunContext({}, ledger.recovery.lease.fence,
        time.time() + 300, api_keys={"LITELLM_RUN_KEY": "test"})
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await gateway.invoke_model_operation_v2(request, context)
    else:
        result = await gateway.invoke_model_operation_v2(request, context)
        assert result.status == "failed" and result.error_code == "model_not_dispatched"
    assert sum(path.endswith("/budgets/reserve") for path, _ in requests) == 1
    assert len({nonce for _, nonce in requests}) == len(requests)
    row = await ledger.recovery.store.operation_record(ledger.recovery.lease, "gateway:model:unreserved")
    if reservation_committed:
        assert row["state"] == "committed" and row["result"]["status"] == "failed"
    else:
        assert row is None
        assert not any(path.endswith("/transition") for path, _ in requests)
    budget = await ledger.recovery.store.budget("r", "u")
    assert not any(budget["reserved"].values())
    assert not any(budget["used"].values())


async def test_sdk_interruption_keeps_conservative_charge_and_cannot_redispatch(host):
    import asyncio
    from open_deep_research.agentscope_runtime.sandbox_provider import NativeGatewayProvider
    gateway, ledger, _ = host
    calls = []
    async def interrupted(request):
        calls.append(request)
        raise asyncio.CancelledError
    provider = NativeGatewayProvider(api_key="test", base_url="http://provider/v1",
                                     transport=httpx.MockTransport(interrupted))
    gateway.model_gateways["r"] = provider
    request = GatewayModelRequestV2(run_id="r", task_id="t", role="quality_evaluation", stage="researching",
        logical_operation_id="interrupted", model="m", messages=[{"role": "user", "content": "q"}])
    context = GatewayRunContext({}, ledger.recovery.lease.fence, time.time()+300,
                               api_keys={"LITELLM_RUN_KEY": "test"})
    try:
        with pytest.raises(asyncio.CancelledError):
            await gateway.invoke_model_operation_v2(request, context)
        receipt = await gateway.invoke_model_operation_v2(request, context)
        assert receipt.status == "uncertain"
        assert len(calls) == 1
        budget = await ledger.recovery.store.budget("r", "u")
        assert budget["used"]["model_calls"] == 1
        row = await ledger.recovery.store.operation_record(ledger.recovery.lease, "gateway:model:interrupted")
        assert row["actual"] == row["reservation"]
    finally:
        await provider.aclose()


async def test_expired_task_deadline_does_not_dispatch_or_charge_model(host):
    from types import SimpleNamespace

    gateway, ledger, _ = host
    calls = []

    async def progress():
        return {"tasks": {"t": {"status": "completed", "deadline_at": time.time() - 1}}}

    ledger.research_cache = SimpleNamespace(progress=progress)

    class Provider:
        async def complete(self, request):
            calls.append(request.logical_operation_id)
            return GatewayModelOutcomeV2(logical_operation_id=request.logical_operation_id,
                requested_model=request.model, status="completed",
                message={"role": "assistant", "content": "summary"},
                usage={"input_tokens": 3, "output_tokens": 4})

    gateway.model_gateways["r"] = Provider()
    request = GatewayModelRequestV2(run_id="r", task_id="t", role="summarization", stage="researching",
        logical_operation_id="expired-summary", model="m", messages=[{"role": "user", "content": "q"}])
    context = GatewayRunContext({"configurable": {"research_efficiency_mode": "bounded"}},
        ledger.recovery.lease.fence, time.time() + 300, api_keys={"LITELLM_RUN_KEY": "test"})
    for _ in range(2):
        result = await gateway.invoke_model_operation_v2(request, context)
        assert result.status == "failed" and result.error_code == "model_not_dispatched"
    assert calls == []
    budget = await ledger.recovery.store.budget("r", "u")
    assert not any(budget["reserved"].values())
    assert not any(budget["used"].values())


@pytest.mark.parametrize("production_router", [False, True])
async def test_tool_lookup_is_read_only_and_bound_to_original_task(host, production_router):
    from open_deep_research.sandbox.wire import GatewayToolRequestV1
    from open_deep_research.agentscope_runtime.recovery_store import RecoveryConflict
    gateway, ledger, client = host
    if production_router:
        from open_deep_research.sandbox.internal_api import build_internal_sandbox_router
        async def resolve(run_id): return ledger
        app = FastAPI()
        app.include_router(build_internal_sandbox_router(lambda _: None, native_ledger=resolve,
            native_root_key=lambda: gateway.configurable.sandbox_root_signing_key))
        client._transport = httpx.ASGITransport(app)
    request = GatewayToolRequestV1(run_id="r", task_id="t", role="researcher", stage="researching",
        execution_zone="gateway", logical_operation_id="fetch", tool_call_id="c", tool_name="fetch_url",
        arguments={"url": "https://docs.example/api"})
    context = GatewayRunContext({}, ledger.recovery.lease.fence, time.time()+300)
    assert await gateway.lookup_tool_operation(request, context) is None
    reserve = gateway.internal.signed(ToolBudgetReserveRequest, run_id="r", task_id="t", stage="researching",
        fence_token=context.fence_token, logical_operation_id="fetch", tool_name="fetch_url",
        fetch_calls=1)
    await gateway.internal.post("/internal/sandbox/budgets/tool-reserve", reserve)
    assert await gateway.lookup_tool_operation(request, context) is None
    row = await ledger.recovery.store.operation_record(ledger.recovery.lease, "gateway:tool:fetch")
    assert row["state"] == "started"  # Reading an in-flight operation must not quarantine it.
    outcome = GatewayToolOutcomeV1(logical_operation_id="fetch", tool_call_id="c", status="failed",
        error={"error_type": "unknown", "message": "known extraction failure"})
    settle = gateway.internal.signed(ToolBudgetSettleRequest, run_id="r", fence_token=context.fence_token,
        logical_operation_id="fetch", outcome=outcome.model_dump(mode="json"), fetch_calls=1)
    await gateway.internal.post("/internal/sandbox/budgets/tool-settle", settle)
    before = await ledger.recovery.store.budget("r", "u")
    assert await gateway.lookup_tool_operation(request, context) == outcome
    assert await gateway.lookup_tool_operation(request, context) == outcome
    assert await ledger.recovery.store.budget("r", "u") == before
    with pytest.raises(RecoveryConflict):
        await gateway.lookup_tool_operation(request.model_copy(update={"task_id": "other-task"}), context)


@pytest.mark.parametrize("json_supported", [True, False])
@pytest.mark.parametrize("attempts", [2, 3])
async def test_structured_format_repair_uses_same_schema_budget_and_physical_ledger(host, json_supported, attempts):
    import json
    from sqlalchemy import update
    from pydantic import BaseModel, SecretStr
    from open_deep_research.agentscope_runtime.gateway import SandboxBinding, SandboxChatModel
    from open_deep_research.agentscope_runtime.sandbox_provider import NativeGatewayProvider
    gateway, ledger, _ = host
    async with ledger.recovery.store.engine.begin() as conn:
        await conn.execute(update(ledger.recovery.store.runs).values(limits={"model_calls": 3}))
    physical = []
    def provider_http(request):
        body = json.loads(request.content)
        physical.append(body)
        if body.get("response_format"):
            assert body["response_format"] == {"type": "json_object"}
            assert not body.get("tools") and not body.get("tool_choice")
            assert '"value"' in str(body["messages"][-1]["content"])
            if not json_supported:
                return httpx.Response(400, json={"error": {"message": "JSON mode unsupported"}})
            message = {"role": "assistant", "content": '{"value":2}'}
        else:
            message = {"role": "assistant", "tool_calls": [{"id": "c", "type": "function",
                "function": {"name": "__insightforge_structured_output",
                             "arguments": '{"value":0,"value":1}' if len(physical) == 1 else '{"value":2}'}}]}
        return httpx.Response(200, json={"id": "response", "object": "chat.completion", "created": 1,
            "model": "fixture", "choices": [{"index": 0, "message": message,
                "finish_reason": "tool_calls" if "tool_calls" in message else "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
    provider = NativeGatewayProvider(api_key="fixture", base_url="http://provider/v1",
                                     transport=httpx.MockTransport(provider_http))
    gateway.model_gateways["r"] = provider
    async def transport(request):
        wire = GatewayModelRequestV2.model_validate_json(request.content)
        context = GatewayRunContext({}, ledger.recovery.lease.fence, time.time()+300,
            api_keys={"LITELLM_RUN_KEY": "fixture"})
        result = await gateway.invoke_model_operation_v2(wire, context)
        assert await gateway.invoke_model_operation_v2(wire, context) == result
        return httpx.Response(200, json=result.model_dump(mode="json"))
    class Answer(BaseModel):
        value: int
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport), base_url="http://gateway") as client:
            model = SandboxChatModel(binding=SandboxBinding("http://gateway", "r", "t", "quality_evaluation",
                "researching", SecretStr("fixture")), model="fixture", client=client, structured_attempts=attempts)
            if not json_supported and attempts == 2:
                with pytest.raises(json.JSONDecodeError, match="duplicate"):
                    await model.generate_structured_output([UserMsg("user", "q")], Answer)
            else:
                answer = await model.generate_structured_output([UserMsg("user", "q")], Answer)
                assert answer.content == {"value": 2}
        assert len(physical) == (2 if json_supported else attempts)
        budget = await ledger.recovery.store.budget("r", "u")
        assert budget["used"]["model_calls"] == len(physical)
        completed = 1 if not json_supported and attempts == 2 else 2
        assert budget["used"]["input_tokens"] == completed * 10
        assert budget["used"]["output_tokens"] == completed * 5
        assert not any(budget["reserved"].values())
    finally:
        await provider.aclose()


@pytest.mark.parametrize('production_router', [False, True])
async def test_deadline_refusal_crosses_gateway_without_dispatch_or_orphan_receipt(host, production_router):
    from pydantic import SecretStr
    from sqlalchemy import update
    from open_deep_research.agentscope_runtime.gateway import SandboxBinding, SandboxChatModel
    from open_deep_research.budgets import DeadlineExceeded

    gateway, ledger, host_client = host
    if production_router:
        from open_deep_research.sandbox.internal_api import build_internal_sandbox_router
        async def resolve(run_id): return ledger
        app = FastAPI()
        app.include_router(build_internal_sandbox_router(lambda _:None,
            native_ledger=resolve, native_root_key=lambda:gateway.configurable.sandbox_root_signing_key))
        host_client._transport = httpx.ASGITransport(app)
    paths = []
    post = gateway.internal.post

    async def expire_before_reserve(path, request):
        paths.append(path)
        if path.endswith('/budgets/reserve'):
            async with ledger.recovery.store.engine.begin() as conn:
                await conn.execute(update(ledger.recovery.store.runs).values(deadline=time.time()-1))
        return await post(path, request)

    gateway.internal.post = expire_before_reserve
    class Provider:
        async def complete(self, request):
            pytest.fail("An expired reservation must never dispatch a physical call")
    gateway.model_gateways['r'] = Provider()

    async def transport(request):
        wire = GatewayModelRequestV2.model_validate_json(request.content)
        result = await gateway.invoke_model_operation_v2(wire,
            GatewayRunContext({}, ledger.recovery.lease.fence, time.time()+300,
                              api_keys={'LITELLM_RUN_KEY':'fixture'}))
        assert result.status == 'failed' and result.error_code == 'deadline_exceeded'
        return httpx.Response(200, json=result.model_dump(mode='json'))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), base_url='http://gateway') as client:
        model = SandboxChatModel(binding=SandboxBinding('http://gateway','r','t','researcher','researching',SecretStr('fixture')),
            model='m', client=client, stream=False)
        with pytest.raises(DeadlineExceeded):
            await ledger.recovery.model('researcher',[],lambda:model(messages=[UserMsg('user','question')]))
    assert paths.count('/internal/sandbox/budgets/reserve') == 1
    assert '/internal/sandbox/operations/transition' not in paths
    assert await ledger.recovery.store.operation_record(ledger.recovery.lease,'outside:pipeline:model:researcher:0') is None
    budget = await ledger.recovery.store.budget('r','u')
    assert not any(budget['reserved'].values()) and not budget['used']


@pytest.mark.parametrize("version", ["quality-gate-v4", "quality-gate-v5"])
async def test_gateway_extraction_owns_only_sql_task_requirements(host, tmp_path, version):
    from types import SimpleNamespace
    from open_deep_research.agentscope_runtime.efficiency import ResearchCache
    from open_deep_research.agentscope_runtime.web_tools import create_web_pipeline

    gateway, ledger, _ = host
    ledger.config = {"metadata": {"quality_policy_version": version}}
    ledger.research_cache = ResearchCache(ledger.recovery, tmp_path)
    contract = {"requirements": [
        {"requirement_id": "q", "text": "dense fusion", "subject": "Qdrant", "kind": "factual"},
        {"requirement_id": "m", "text": "sparse fusion", "subject": "Milvus", "kind": "factual"},
    ]}
    ledger.recovery.snapshot.coverage_contract = contract
    await ledger.research_cache.progress({"tasks": {"t": {"requirements": ["m"]}, "sibling": {"requirements": ["q"]}}})
    reserved = await _tool_reserve(gateway, ledger, "owned-research", tool_name="web_research", idempotent=True)
    assert reserved["coverage_contract"] == contract  # Source admission keeps the authority.
    if version == "quality-gate-v5":
        assert reserved["evidence_requirement_ids"] == ["m"]
    else:
        assert "evidence_requirement_ids" not in reserved
    config = {"metadata": {**ledger.config["metadata"], "coverage_contract": reserved["coverage_contract"],
                           **({"evidence_requirement_ids": reserved["evidence_requirement_ids"]} if "evidence_requirement_ids" in reserved else {})}}
    pipeline = create_web_pipeline(config, SimpleNamespace(), SimpleNamespace())
    ids = {row["requirement_id"] for row in pipeline.evidence_context["requirements"]}
    assert ids == ({"m"} if version == "quality-gate-v5" else {"q", "m"})
    if version == "quality-gate-v5":
        assert pipeline.evidence_context["objective"] == "Milvus: sparse fusion"


async def test_shadow_receipt_replays_diagnostics_and_fetch_usage_once(host):
    gateway, ledger, _ = host
    ledger.config = {"configurable": {"web_pipeline_mode": "shadow"}}
    await _tool_reserve(gateway, ledger, "shadow-search", tool_name="web_search", idempotent=True)
    diagnostics = {"shadow": {"status": "completed", "fetch_calls": 2, "evidence_count": 3}}
    outcome = GatewayToolOutcomeV1(logical_operation_id="shadow-search", tool_call_id="call", status="completed", output="primary search summary")
    settle = gateway.internal.signed(ToolBudgetSettleRequest, run_id="r", fence_token=ledger.recovery.lease.fence,
        logical_operation_id="shadow-search", outcome=outcome.model_dump(), fetch_calls=2, diagnostics=diagnostics)
    await gateway.internal.post("/internal/sandbox/budgets/tool-settle", settle)
    replay = await _tool_reserve(gateway, ledger, "shadow-search", tool_name="web_search", idempotent=True)
    assert replay["replayed"] is True
    assert replay["result"]["web_diagnostics"] == diagnostics
    assert replay["result"]["outcome"]["output"] == "primary search summary"
    budget = await ledger.recovery.store.budget("r", "u")
    assert budget["used"] == {"tool_calls": 1, "fetch_calls": 2}


async def test_unsettled_search_is_unknown_even_when_the_tool_is_read_only(host):
    gateway, ledger, client = host
    await _tool_reserve(gateway, ledger, "unknown-search", tool_name="web_search", idempotent=True)
    request = gateway.internal.signed(ToolBudgetReserveRequest, run_id="r", task_id="t",
        fence_token=ledger.recovery.lease.fence, stage="researching", logical_operation_id="unknown-search",
        tool_name="web_search", idempotent=True)
    response = await client.post("/internal/sandbox/budgets/tool-reserve", json=request.model_dump(mode="json"))
    assert response.status_code == 409
    assert response.json()["detail"] == "tool_operation_unknown"


async def test_sql_grants_only_remaining_fetches_and_allows_cached_reads(host):
    gateway, ledger, _ = host
    await ledger.recovery.store.save(ledger.recovery.lease, ledger.recovery.snapshot, limits={"fetch_calls": 1})
    async def reserve(key, name, count):
        request = gateway.internal.signed(ToolBudgetReserveRequest, run_id="r", task_id="t",
            fence_token=ledger.recovery.lease.fence, stage="researching", logical_operation_id=key,
            tool_name=name, idempotent=True, fetch_calls=count)
        return await gateway.internal.post("/internal/sandbox/budgets/tool-reserve", request)
    assert (await reserve("fetch-first", "web_research", 5))["fetch_grant"] == 1
    settle = gateway.internal.signed(ToolBudgetSettleRequest, run_id="r", fence_token=ledger.recovery.lease.fence,
        logical_operation_id="fetch-first", fetch_calls=1, outcome={"status": "completed"})
    await gateway.internal.post("/internal/sandbox/budgets/tool-settle", settle)
    assert (await reserve("cached-page", "fetch_url", 0))["fetch_grant"] == 0
    ledger.config = {"configurable": {"web_pipeline_mode": "shadow"}}
    assert (await reserve("shadow-no-budget", "web_search", 2))["fetch_grant"] == 0
    budget = await ledger.recovery.store.budget("r", "u")
    assert budget["used"]["fetch_calls"] == 1 and budget["reserved"]["fetch_calls"] == 0


async def test_known_transport_failure_refund_matches_the_sql_receipt(host, monkeypatch):
    from types import SimpleNamespace
    from test_web_upgrade import Models, config, context
    from open_deep_research.agentscope_runtime.web_tools import FetchUrlInput, WebFetchLedger, fetch_url_tool
    from open_deep_research.web import pipeline
    from open_deep_research.web.models import FetchResult

    async def fail(source, settings, **kwargs):
        return pipeline.RawFetch(FetchResult(candidate_id=source.candidate_id,
            requested_url=source.canonical_url, failure_class="timeout"))
    monkeypatch.setattr(pipeline, "fetch_local", fail)
    gateway, ledger, _ = host
    cfg = config(fetch_backend_order=["local"])
    memory = WebFetchLedger()
    tool = fetch_url_tool(lambda: cfg, Models(), memory)
    result = await tool.call(FetchUrlInput(url="https://docs.example/failed"), context(cfg))
    assert result.metadata["physical_fetches"] == 1
    assert result.metadata["transport_failed_fetches"] == 1
    charge = gateway._governed_fetch_calls(SimpleNamespace(result=result))
    await _tool_reserve(gateway, ledger, "failed-fetch", tool_name="fetch_url", idempotent=True)
    settle = gateway.internal.signed(ToolBudgetSettleRequest, run_id="r", fence_token=ledger.recovery.lease.fence,
        logical_operation_id="failed-fetch", fetch_calls=charge, outcome={"status": "completed"})
    await gateway.internal.post("/internal/sandbox/budgets/tool-settle", settle)
    budget = await ledger.recovery.store.budget("r", "u")
    assert budget["used"]["fetch_calls"] == memory._run_attempts["web-upgrade-test"] == 0


async def test_scope_rejection_refunds_fetch_but_keeps_tool_cost_and_unknown_reservations(host):
    from types import SimpleNamespace
    from open_deep_research.tools.governance import ToolError, ToolErrorType
    gateway, ledger, _ = host
    error = ToolError(error_type=ToolErrorType.validation_error, tool_name="fetch_url",
        message="Outside the approved source plan", detail={"reason_code": "source_scope_denied"})
    charge = gateway._governed_fetch_calls(SimpleNamespace(error=error, result=None))
    await _tool_reserve(gateway, ledger, "scope-denied", tool_name="fetch_url", idempotent=True)
    request = gateway.internal.signed(ToolBudgetSettleRequest, run_id="r", fence_token=ledger.recovery.lease.fence,
        logical_operation_id="scope-denied", fetch_calls=charge, outcome={"status": "failed"})
    await gateway.internal.post("/internal/sandbox/budgets/tool-settle", request)
    budget = await ledger.recovery.store.budget("r", "u")
    assert budget["used"] == {"tool_calls": 1, "fetch_calls": 0}
    assert gateway._governed_fetch_calls(SimpleNamespace(error=None, result=None)) is None


async def test_native_server_search_receipt_keeps_citations_and_usage_on_replay(host):
    from open_deep_research.agentscope_runtime.sandbox_provider import NativeGatewayProvider
    from open_deep_research.sandbox.wire import ServerSearchRequest
    from test_server_search_models import openai_events, sse

    gateway, ledger, _ = host
    calls = []
    async def transport(request):
        calls.append(request.url.path)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse(openai_events("fixture")))
    provider = NativeGatewayProvider(api_key="fixture-run-key", base_url="https://proxy.example/v1", transport=httpx.MockTransport(transport))
    gateway.model_gateways["r"] = provider
    request = GatewayModelRequestV2(run_id="r", task_id="t", stage="researching", role="openai_search",
        model="fixture", logical_operation_id="native-search", messages=[{"role": "user", "content": "q"}],
        server_search=ServerSearchRequest(provider="openai", query="q"), max_output_tokens=100)
    context = GatewayRunContext({}, ledger.recovery.lease.fence, time.time()+300, api_keys={"LITELLM_RUN_KEY": "fixture-run-key"})
    try:
        first = await gateway.invoke_model_operation_v2(request, context)
        again = await gateway.invoke_model_operation_v2(request, context)
        assert first.status == "completed" and first == again
        assert first.search_result["sources"][0]["url"] == "https://docs.example/api"
        assert len(calls) == 1
        budget = await ledger.recovery.store.budget("r", "u")
        assert budget["used"]["model_calls"] == 1
        assert budget["used"]["input_tokens"] == 7
    finally: await provider.aclose()


@pytest_asyncio.fixture
async def host(tmp_path):
    store = RecoveryStore("sqlite+aiosqlite:///" + (tmp_path / "ledger.db").as_posix())
    await store.create_tables()
    await store.create_run(
        "u",
        ResearchSnapshot(run_id="r", config_fingerprint="f"),
        limits={"model_calls": 1},
    )
    recovery = await RecoverySession.open(store, "r", "u")
    recovery.model_accounting = "gateway"
    ledger = SQLGatewayLedger(recovery, {})
    root = base64.b64encode(b"x" * 32).decode()
    gateway = GatewayRuntime(Configuration(sandbox_root_signing_key=root))
    app = FastAPI()

    async def resolve(run):
        return resolvable.get(run)

    resolvable = {"r": ledger}
    ledger.resolvable_runs = resolvable
    app.include_router(build_gateway_ledger_router(resolve, root))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://host"
    ) as client:

        async def post(path, request):
            response = await client.post(path, json=request.model_dump(mode="json"))
            response.raise_for_status()
            return response.json()

        gateway.internal.post = post
        yield gateway, ledger, client
    await ledger.recovery.close()
    await store.aclose()


async def test_atomic_receipt_replay_and_no_outer_double_charge(host):
    gateway, ledger, _client = host
    calls = []

    class Provider:
        async def complete(self, request):
            calls.append(request.logical_operation_id)
            return GatewayModelOutcomeV2(
                logical_operation_id=request.logical_operation_id,
                requested_model=request.model,
                status="completed",
                message={"role": "assistant", "content": "answer"},
                usage={"input_tokens": 3, "output_tokens": 4},
            )

    gateway.model_gateways["r"] = Provider()

    async def call():
        request = GatewayModelRequestV2(
            run_id="r",
            task_id="pipeline",
            role="researcher",
            stage="researching",
            logical_operation_id="stable",
            model="model",
            messages=[{"role": "user", "content": "q"}],
        )
        outcome = await gateway.invoke_model_operation_v2(
            request,
            GatewayRunContext(
                {},
                ledger.recovery.lease.fence,
                time.time() + 300,
                api_keys={"LITELLM_RUN_KEY": "test"},
            ),
        )
        assert outcome.status == "completed"
        return ChatResponse(content=[TextBlock(text="answer")], is_last=True)

    async def crash(point):
        if point == "effect_returned":
            raise RuntimeError("host killed after gateway receipt")

    first = ledger.recovery
    first.failpoint = crash
    with pytest.raises(RuntimeError, match="host killed"):
        await first.model("researcher", [UserMsg("user", "q")], call)
    await first.close()
    second = await RecoverySession.open(first.store, "r", "u")
    second.model_accounting = "gateway"
    ledger.recovery = second
    await second.model("researcher", [UserMsg("user", "q")], call)
    budget = await second.store.budget("r", "u")
    assert budget["used"] == {"model_calls": 1, "input_tokens": 3, "output_tokens": 4}
    assert all(amount == 0 for amount in budget["reserved"].values())
    assert calls == ["stable"]


async def test_v2_receipt_lookup_is_read_only_and_checks_request_digest(host):
    from open_deep_research.agentscope_runtime.recovery_store import RecoveryConflict
    gateway, ledger, _ = host
    calls = []
    class Provider:
        async def complete(self, request):
            calls.append(request.logical_operation_id)
            return GatewayModelOutcomeV2(logical_operation_id=request.logical_operation_id,
                requested_model=request.model, status="completed", message={"role": "assistant", "content": "answer"})
    gateway.model_gateways["r"] = Provider()
    request = GatewayModelRequestV2(run_id="r", task_id="pipeline", role="researcher", stage="researching",
        logical_operation_id="stable", model="model", messages=[{"role": "user", "content": "q"}])
    context = GatewayRunContext({}, ledger.recovery.lease.fence, time.time()+300, api_keys={"LITELLM_RUN_KEY": "test"})
    assert await gateway.lookup_model_operation_v2(request, context) is None
    assert calls == []
    result = await gateway.invoke_model_operation_v2(request, context)
    assert await gateway.lookup_model_operation_v2(request, context) == result
    assert calls == ["stable"]
    with pytest.raises(RecoveryConflict, match="input changed"):
        await gateway.lookup_model_operation_v2(request.model_copy(update={"messages": [{"role": "user", "content": "different"}]}), context)
    assert calls == ["stable"]


async def test_signed_boundary_rejects_nonce_replay_and_old_fence(host):
    gateway, ledger, client = host
    body = gateway.internal.signed(
        OperationGetRequest,
        run_id="r",
        fence_token=ledger.recovery.lease.fence,
        logical_operation_id="none",
    )
    path = "/internal/sandbox/operations/get"
    assert (
        await client.post(path, json=body.model_dump(mode="json"))
    ).status_code == 200
    assert (
        await client.post(path, json=body.model_dump(mode="json"))
    ).status_code == 401
    stale = gateway.internal.signed(
        OperationGetRequest, run_id="r", fence_token=0, logical_operation_id="none"
    )
    assert (
        await client.post(path, json=stale.model_dump(mode="json"))
    ).status_code == 409


async def test_unknown_dispatch_keeps_reservation(host):
    gateway, ledger, _client = host
    request = gateway.internal.signed(
        BudgetReserveRequest,
        run_id="r",
        task_id="t",
        fence_token=ledger.recovery.lease.fence,
        stage="researching",
        logical_operation_id="lost",
        physical_attempt_id="a",
        model_name="m",
        estimated_input_tokens=10,
        estimated_output_tokens=20,
        request_digest="original-input",
    )
    await ledger.reserve(request)
    record = await ledger.lookup("lost", "original-input")
    assert record["operation"]["status"] == "dispatched"
    budget = await ledger.recovery.store.budget("r", "u")
    assert budget["reserved"]["model_calls"] == 1 and budget["used"] == {}
    with pytest.raises(ValueError, match="input changed"):
        await ledger.lookup("lost", "changed-input")


async def test_gateway_budget_rejection_becomes_native_approval(host):
    from pydantic import SecretStr

    from open_deep_research.agentscope_runtime.gateway import (
        SandboxBinding,
        SandboxChatModel,
    )
    from open_deep_research.agentscope_runtime.recovery import ApprovalPending

    gateway, ledger, _client = host
    reservation = gateway.internal.signed(
        BudgetReserveRequest,
        run_id="r",
        task_id="t",
        fence_token=ledger.recovery.lease.fence,
        stage="researching",
        logical_operation_id="other",
        physical_attempt_id="attempt",
        model_name="m",
        estimated_input_tokens=2,
        estimated_output_tokens=2,
        request_digest="other-payload",
    )
    await ledger.reserve(reservation)

    class Provider:
        async def complete(self, request):
            pytest.fail("budget rejection must precede physical execution")

    gateway.model_gateways["r"] = Provider()
    identifiers = []

    async def transport(request):
        import json

        wire = GatewayModelRequestV2.model_validate(json.loads(request.content))
        identifiers.append(wire.logical_operation_id)
        result = await gateway.invoke_model_operation_v2(
            wire,
            GatewayRunContext(
                {},
                ledger.recovery.lease.fence,
                time.time() + 300,
                api_keys={"LITELLM_RUN_KEY": "test"},
            ),
        )
        return httpx.Response(200, json=result.model_dump(mode="json"))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), base_url="http://gateway"
    ) as client:
        model = SandboxChatModel(
            binding=SandboxBinding(
                "http://gateway",
                "r",
                "t",
                "researcher",
                "researching",
                SecretStr("capability"),
            ),
            model="m",
            client=client,
            stream=False,
        )
        with pytest.raises(ApprovalPending) as error:
            await ledger.recovery.model(
                "researcher", [], lambda: model(messages=[UserMsg("user", "q")])
            )
        assert error.value.payload["dimension"] == "model_calls"
        # A new journal scope uses the same transport identity after recovery.
        from open_deep_research.agentscope_runtime.gateway import (
            gateway_operation_scope,
        )
        from open_deep_research.budgets import BudgetExhausted

        with gateway_operation_scope("outside:pipeline:model:researcher:0"), pytest.raises(BudgetExhausted):
            await model(messages=[UserMsg("user", "q")])
        assert len(identifiers) == 2 and identifiers[0] == identifiers[1]


async def _tool_reserve(gateway, ledger, operation_id, *, tool_name, idempotent):
    request = gateway.internal.signed(
        ToolBudgetReserveRequest,
        run_id="r",
        task_id="t",
        fence_token=ledger.recovery.lease.fence,
        stage="researching",
        logical_operation_id=operation_id,
        tool_name=tool_name,
        idempotent=idempotent,
    )
    response = await gateway.internal.post(
        "/internal/sandbox/budgets/tool-reserve", request
    )
    return response


async def test_tool_ledger_settles_actual_fetches_and_replays_outcome(host):
    """工具物理执行统一计一次：实测抓取数结算，已提交回执直接复用。"""
    gateway, ledger, client = host
    reserved = await _tool_reserve(
        gateway, ledger, "tool-op-1", tool_name="web_research", idempotent=True
    )
    assert reserved == {"replayed": False}
    budget = await ledger.recovery.store.budget("r", "u")
    assert budget["reserved"] == {"tool_calls": 1, "fetch_calls": 1}

    outcome = GatewayToolOutcomeV1(
        logical_operation_id="tool-op-1",
        tool_call_id="call-1",
        status="completed",
        output="evidence",
    )
    settle = gateway.internal.signed(
        ToolBudgetSettleRequest,
        run_id="r",
        fence_token=ledger.recovery.lease.fence,
        logical_operation_id="tool-op-1",
        outcome=outcome.model_dump(mode="json"),
        fetch_calls=3,
    )
    assert (
        await gateway.internal.post(
            "/internal/sandbox/budgets/tool-settle", settle
        )
        == {"status": "settled"}
    )
    budget = await ledger.recovery.store.budget("r", "u")
    assert budget["used"] == {"tool_calls": 1, "fetch_calls": 3}
    assert all(amount == 0 for amount in budget["reserved"].values())

    # 崩溃后重试：预留端点返回已提交回执，工具不再次执行。
    replayed = await _tool_reserve(
        gateway, ledger, "tool-op-1", tool_name="web_research", idempotent=True
    )
    assert replayed["replayed"] is True
    assert GatewayToolOutcomeV1.model_validate(
        replayed["result"]["outcome"]
    ) == outcome
    budget = await ledger.recovery.store.budget("r", "u")
    assert budget["used"] == {"tool_calls": 1, "fetch_calls": 3}


async def test_tool_ledger_missing_fetch_report_keeps_reservation(host):
    """抓取数缺失时保持保守预留，不当零。"""
    gateway, ledger, _client = host
    await _tool_reserve(
        gateway, ledger, "tool-op-2", tool_name="fetch_url", idempotent=True
    )
    settle = gateway.internal.signed(
        ToolBudgetSettleRequest,
        run_id="r",
        fence_token=ledger.recovery.lease.fence,
        logical_operation_id="tool-op-2",
        outcome={"status": "completed"},
    )
    await gateway.internal.post("/internal/sandbox/budgets/tool-settle", settle)
    budget = await ledger.recovery.store.budget("r", "u")
    assert budget["used"] == {"tool_calls": 1, "fetch_calls": 1}


async def test_non_idempotent_tool_retry_is_quarantined_not_replayed(host):
    """非幂等工具在"已执行未结算"窗口重试进入未知隔离，不自动重放。"""
    gateway, ledger, client = host
    await _tool_reserve(
        gateway, ledger, "tool-op-3", tool_name="publish_report", idempotent=False
    )
    request = gateway.internal.signed(
        ToolBudgetReserveRequest,
        run_id="r",
        task_id="t",
        fence_token=ledger.recovery.lease.fence,
        stage="researching",
        logical_operation_id="tool-op-3",
        tool_name="publish_report",
        idempotent=False,
    )
    response = await client.post(
        "/internal/sandbox/budgets/tool-reserve", json=request.model_dump(mode="json")
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "tool_operation_unknown"


async def test_tool_budget_exhaustion_maps_to_dimension_error(host):
    gateway, ledger, client = host
    store = ledger.recovery.store
    await store.create_run(
        "u2",
        ResearchSnapshot(run_id="r2", config_fingerprint="f"),
        limits={"tool_calls": 1},
    )
    second = await RecoverySession.open(store, "r2", "u2")
    try:
        original = ledger.recovery
        ledger.recovery = second
        ledger.resolvable_runs["r2"] = ledger
        await _tool_reserve(
            gateway, ledger, "tool-a", tool_name="web_research", idempotent=True
        )
        # 第二次调用超出 tool_calls 上限：429 携带维度。
        second_store_budget = await second.store.budget("r2", "u2")
        assert second_store_budget["reserved"] == {"tool_calls": 1, "fetch_calls": 1}
        request = gateway.internal.signed(
            ToolBudgetReserveRequest,
            run_id="r2",
            task_id="t",
            fence_token=second.lease.fence,
            stage="researching",
            logical_operation_id="tool-b",
            tool_name="web_research",
            idempotent=True,
        )
        response = await client.post(
            "/internal/sandbox/budgets/tool-reserve", json=request.model_dump(mode="json")
        )
        assert response.status_code == 429
        assert response.json()["detail"] == "budget_exhausted:tool_calls"
    finally:
        ledger.recovery = original
        await second.close()


async def test_failed_model_transition_settles_billed_usage_not_zero(host):
    """失败物理调用携带的已计费 usage 计入账本；无账单才零结算。"""
    gateway, ledger, _client = host
    store = ledger.recovery.store
    await store.create_run(
        "u3",
        ResearchSnapshot(run_id="r3", config_fingerprint="f"),
        limits={"model_calls": 3},
    )
    third = await RecoverySession.open(store, "r3", "u3")
    try:
        original = ledger.recovery
        ledger.recovery = third
        reservation = gateway.internal.signed(
            BudgetReserveRequest,
            run_id="r3",
            task_id="t",
            fence_token=third.lease.fence,
            stage="researching",
            logical_operation_id="failed-op",
            physical_attempt_id="p1",
            model_name="m",
            estimated_input_tokens=5,
            estimated_output_tokens=6,
            request_digest="payload",
        )
        await ledger.reserve(reservation)
        transition = gateway.internal.signed(
            OperationTransitionRequest,
            run_id="r3",
            fence_token=third.lease.fence,
            logical_operation_id="failed-op",
            status="failed",
            outcome=GatewayModelOutcomeV2(
                logical_operation_id="failed-op",
                status="failed",
                requested_model="m",
                usage={"input_tokens": 11, "output_tokens": 7},
                error_code="length",
            ).model_dump(mode="json"),
            error_type="length",
        )
        await ledger.transition(transition)
        budget = await store.budget("r3", "u3")
        assert budget["used"]["input_tokens"] == 11
        assert budget["used"]["output_tokens"] == 7

        # 无账单的确定拒绝：零结算释放预留。
        reservation = gateway.internal.signed(
            BudgetReserveRequest,
            run_id="r3",
            task_id="t",
            fence_token=third.lease.fence,
            stage="researching",
            logical_operation_id="rejected-op",
            physical_attempt_id="p2",
            model_name="m",
            estimated_input_tokens=5,
            estimated_output_tokens=6,
            request_digest="payload",
        )
        await ledger.reserve(reservation)
        transition = gateway.internal.signed(
            OperationTransitionRequest,
            run_id="r3",
            fence_token=third.lease.fence,
            logical_operation_id="rejected-op",
            status="failed",
            outcome=GatewayModelOutcomeV2(
                logical_operation_id="rejected-op",
                status="failed",
                requested_model="m",
                error_code="authentication",
            ).model_dump(mode="json"),
            error_type="authentication",
        )
        await ledger.transition(transition)
        budget = await store.budget("r3", "u3")
        assert budget["used"]["input_tokens"] == 11  # 无账单失败未追加
        assert budget["reserved"].get("model_calls", 0) == 0
    finally:
        ledger.recovery = original
        await third.close()


@pytest.mark.parametrize("status", ["completed", "failed"])
async def test_partial_gateway_usage_keeps_unknown_dimension(host, status):
    gateway, ledger, _ = host
    await ledger.reserve(gateway.internal.signed(
        BudgetReserveRequest, run_id="r", fence_token=ledger.recovery.lease.fence,
        task_id="t", stage="researching", logical_operation_id="partial",
        physical_attempt_id="p", model_name="m", estimated_input_tokens=8,
        estimated_output_tokens=10, request_digest="partial",
    ))
    transition = gateway.internal.signed(
        OperationTransitionRequest, run_id="r", fence_token=ledger.recovery.lease.fence,
        logical_operation_id="partial", status=status,
        outcome={"requested_model": "m", "usage": {"input_tokens": 0}},
    )
    await ledger.transition(transition)
    await ledger.transition(transition)
    used = (await ledger.recovery.store.budget("r", "u"))["used"]
    assert used == {"model_calls": 1, "input_tokens": 0, "output_tokens": 10}


@pytest.mark.parametrize("failure", ["context", "connection"])
async def test_provider_failure_semantics_survive_native_sql_and_wire(host, failure):
    import json
    from pydantic import SecretStr
    from open_deep_research.agentscope_runtime.gateway import SandboxBinding, SandboxChatModel, GatewayCallError
    from open_deep_research.agentscope_runtime.sandbox_provider import NativeGatewayProvider
    from open_deep_research.models.errors import is_token_limit_exceeded

    gateway, ledger, _client = host
    physical_calls, outcomes = [], []

    def provider_http(request):
        physical_calls.append(request)
        if failure == "connection":
            raise httpx.ReadError("PRIVATE-UPSTREAM-TEXT", request=request)
        return httpx.Response(400, json={"error": {"code": "context_length_exceeded",
                                                  "message": "PRIVATE-UPSTREAM-TEXT"}})

    provider = NativeGatewayProvider(api_key="fixture", base_url="https://fixture.invalid/v1",
                                     transport=httpx.MockTransport(provider_http))
    gateway.model_gateways["r"] = provider

    async def transport(request):
        wire = GatewayModelRequestV2.model_validate(json.loads(request.content))
        context = GatewayRunContext({}, ledger.recovery.lease.fence, time.time() + 300,
                                    api_keys={"LITELLM_RUN_KEY": "fixture"})
        outcome = await gateway.invoke_model_operation_v2(wire, context)
        # Replay the exact physical operation from its SQL receipt, with no second charge.
        assert await gateway.invoke_model_operation_v2(wire, context) == outcome
        outcomes.append(outcome)
        assert "PRIVATE-UPSTREAM-TEXT" not in outcome.model_dump_json()
        return httpx.Response(200, json=outcome.model_dump(mode="json"))

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport), base_url="http://gateway") as client:
            model = SandboxChatModel(binding=SandboxBinding("http://gateway", "r", "t", "researcher",
                                                            "researching", SecretStr("capability")),
                                     model="research", client=client, stream=False)
            with pytest.raises(GatewayCallError) as raised:
                await model(messages=[UserMsg("user", "Research")])
            assert is_token_limit_exceeded(raised.value) is (failure == "context")
            assert raised.value.uncertain is (failure == "connection")
        assert len(physical_calls) == 1
        assert outcomes[0].status == ("failed" if failure == "context" else "uncertain")
    finally:
        await provider.aclose()
