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
)
from open_deep_research.sandbox.wire import GatewayModelOutcomeV2, GatewayModelRequestV2

pytestmark = pytest.mark.asyncio


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
        return ledger if run == "r" else None

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
