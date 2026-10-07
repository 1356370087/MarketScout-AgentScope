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
