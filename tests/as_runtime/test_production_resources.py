"""Production resource lifecycle uses real capabilities, not process env mutation."""

import asyncio
import base64
import time
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from test_recovery import (
    create,
    store,  # noqa: F401
)

from open_deep_research.agentscope_runtime import production_resources as resources
from open_deep_research.agentscope_runtime.recovery import RecoverySession
from open_deep_research.agentscope_runtime.run_config import RunConfig
from open_deep_research.sandbox.crypto import SandboxDerivedKeys, decode_task_token

# ruff: noqa: F811 -- imported pytest fixture
pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    for key in list(os.environ):
        if key.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
            monkeypatch.delenv(key)


def config():
    cfg = {
        "model_backend": "litellm",
        "sandbox_enabled": True,
        "enable_async_research": True,
        "enable_memory": False,
        "sandbox_root_signing_key": base64.b64encode(b"k" * 32).decode(),
        "sandbox_gateway_url": "http://gateway:8081",
    }
    for field, fallback, _ in resources.ROLES.values():
        cfg[field] = "fixture"
        cfg[fallback] = "fixture"
    cfg["report_review_model"] = "fixture"
    entry = {
        "context_window": 32000,
        "max_output_tokens": 2000,
        "input_cost_per_token": 0.001,
        "output_cost_per_token": 0.002,
    }
    cfg["model_catalog_snapshot"] = {
        name: {**entry, "model_name": name} for name in ("fixture", "if-fallback-v1")
    }
    return {"configurable": cfg}


@pytest.mark.parametrize(
    "fail_registration, renew, late_charge",
    [(False, False, False), (True, False, False), (False, True, False), (False, False, True)],
)
@pytest.mark.parametrize("documents", [False, True])
async def test_owner_resource_registration_cleanup_and_no_secret_in_config(
    store, monkeypatch, tmp_path, fail_registration, renew, late_charge, documents
):
    events = []
    settings = SimpleNamespace(
        encryption_key="unused", ttl_seconds=4 if renew else 86400
    )
    monkeypatch.setattr(resources.RunKeySettings, "from_env", lambda: settings)
    from open_deep_research.agentscope_runtime import spend_reconciliation

    async def no_bills(*args):
        return {}

    monkeypatch.setattr(spend_reconciliation, "collect_spend", no_bills)
    monkeypatch.setattr(resources, "RunKeySecretStore", lambda *args: object())
    monkeypatch.setattr(resources, "LiteLLMKeyAdminClient", lambda *args: object())

    class Manager:
        def __init__(self, *args):
            self.admin = self

        async def renew(self, lease):
            events.append("renew")
            lease.metadata.expires_at = time.time() + 600
            return lease

        async def ensure(self, **kwargs):
            events.append("key")
            assert kwargs["requested_budget_micro_usd"] == (50 if late_charge else 60)
            assert ("if-embedding-v1" in kwargs["allowed_models"]) is documents
            return SimpleNamespace(
                key="secret-run-key", metadata=SimpleNamespace(expires_at=0)
            )

        async def finalize(self, run_id):
            events.append("block")
            return True

        async def aclose(self):
            events.append("close")

    class Control:
        def __init__(self, cfg):
            pass

        async def register_run(self, **kwargs):
            events.append("register")
            assert kwargs["api_keys"]["LITELLM_RUN_KEY"] == "secret-run-key"
            assert "secret-run-key" not in str(kwargs["frozen_config"])
            if fail_registration:
                raise ConnectionError("ack lost")

        async def unregister_run(self, **kwargs):
            events.append("unregister")

    monkeypatch.setattr(resources, "RunKeyManager", Manager)
    monkeypatch.setattr(resources, "SandboxGatewayControlClient", Control)
    state, lease = await create(store, limits={"cost_micro_usd": 100})
    await store.begin_operation(lease, "settled", "model", {}, reserve={"cost_micro_usd": 40})
    await store.commit_operation(lease, "settled", {}, actual={"cost_micro_usd": 30})
    await store.begin_operation(lease, "unknown", "model", {}, reserve={"cost_micro_usd": 10})
    if late_charge:
        original_transaction = store.transaction
        charged = False

        @asynccontextmanager
        async def late_settlement(current):
            nonlocal charged
            if not charged:
                charged = True
                # A receipt lands just before resource creation takes the row lock.
                await store.commit_operation(lease, "unknown", {}, actual={"cost_micro_usd": 20})
            async with original_transaction(current) as locked:
                yield locked

        monkeypatch.setattr(store, "transaction", late_settlement)
    session = RecoverySession(store, lease, state)
    cfg = config()
    if documents:
        cfg["metadata"] = {"source_selection": {"mode": "documents", "sources": [
            {"type": "document", "id": "doc"}]}}
    run = RunConfig.compile(cfg)
    try:
        async with resources.production_resources(tmp_path)(run, cfg, session) as ports:
            a = ports.model_for("researcher", "a")
            b = ports.model_for("researcher", "b")
            assert a is not b
            keys = SandboxDerivedKeys.from_root(
                cfg["configurable"]["sandbox_root_signing_key"]
            )
            claims = decode_task_token(
                a._binding.token().get_secret_value(), keys.task_token
            )
            assert claims.task_id == "a" and claims.run_id == state.run_id
            assert ports.gateway_accounting
            assert "secret-run-key" not in str(run.snapshot())
            if renew:
                await asyncio.sleep(1.2)
                assert events.count("renew") == 1
                assert events.count("register") == 2
            first = a._binding.token()
            monkeypatch.setattr(
                resources, "time", SimpleNamespace(time=lambda: time.time() + 100000)
            )
            assert a._binding.token().get_secret_value() != first.get_secret_value()
    except ConnectionError:
        assert fail_registration
    assert events == (
        ["key", "register", "renew", "register", "unregister", "block", "close"]
        if renew
        else ["key", "register", "unregister", "block", "close"]
    )
    await session.close()


async def test_child_attaches_without_registering_or_erasing_leader_vault(
    store, monkeypatch, tmp_path
):
    def forbidden(*args):
        raise AssertionError("child cannot own run credentials")

    monkeypatch.setattr(resources, "RunKeyManager", forbidden)
    monkeypatch.setattr(resources, "SandboxGatewayControlClient", forbidden)
    state, lease = await create(store)
    session = RecoverySession(store, lease, state)
    cfg = config()
    async with resources.production_resources(tmp_path, worker_task_id="task-a")(
        RunConfig.compile(cfg), cfg, session
    ) as ports:
        assert ports.model_for("compression", "pipeline")._binding.task_id == "task-a"
        assert ports.team_launcher is None
    await session.close()


@pytest.mark.parametrize("decision", ["allow", "deny", "cancel", "reconnect"])
async def test_native_gateway_approval_keeps_one_tool_operation_pending(
    store, monkeypatch, tmp_path, decision
):
    from open_deep_research.tools.base import ToolExecutionZone, ToolResult
    from open_deep_research.tools.governance import ApprovalPendingError

    state, lease = await create(store)
    session = RecoverySession(store, lease, state)
    cfg = config()
    requested, resolved = asyncio.Event(), asyncio.Event()
    calls = []
    context = SimpleNamespace(config={"metadata": {"task_id": "task-a"}},
                              operation_id="same-operation", tool_call_id="same-call")

    async def call(proxy, input, current, on_progress=None):
        assert current is context
        calls.append(current.operation_id)
        if len(calls) == 1:
            requested.set()
            raise ApprovalPendingError("awaiting decision", approval_id="approval", domain="example.test")
        await resolved.wait()
        if decision == "deny":
            raise RuntimeError("egress_domain_denied")
        return ToolResult(output="approved source")

    monkeypatch.setattr(resources.GatewayToolProxy, "call", call)
    try:
        async with resources.production_resources(tmp_path, worker_task_id="task-a")(
            RunConfig.compile(cfg), cfg, session
        ) as ports:
            tool = SimpleNamespace(execution_zone=ToolExecutionZone.GATEWAY)
            pending = asyncio.create_task(ports.dispatcher(tool, None, context))
            await asyncio.wait_for(requested.wait(), 2)
            await asyncio.sleep(0)
            assert not pending.done(), "a pending approval must not fail the Worker"
            if decision in {"cancel", "reconnect"}:
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
                if decision == "cancel":
                    assert calls == ["same-operation"]
                    return
                # Restart attaches to the same operation, never invents another call.
                pending = asyncio.create_task(ports.dispatcher(tool, None, context))
            resolved.set()
            if decision == "deny":
                with pytest.raises(RuntimeError, match="egress_domain_denied"):
                    await asyncio.wait_for(pending, 2)
            else:
                assert (await asyncio.wait_for(pending, 2)).output == "approved source"
            assert calls == ["same-operation", "same-operation"]
            assert session.problem is None
    finally:
        await session.close()


async def test_native_egress_authority_uses_live_sql_fence(store, tmp_path):
    import httpx
    from fastapi import FastAPI

    from open_deep_research.agentscope_runtime.gateway_ledger import SQLGatewayLedger
    from open_deep_research.sandbox.gateway import GatewayRuntime
    from open_deep_research.sandbox.internal_api import (
        EgressTargetCheckRequest,
        TaskActivityPublishRequest,
        TeamBridgeRequest,
        build_internal_sandbox_router,
    )

    state, lease = await create(store)
    session = RecoverySession(store, lease, state)
    ledger = SQLGatewayLedger(session, {})
    cfg = config()
    cfg["configurable"]["runs_dir"] = str(tmp_path)
    cfg["metadata"] = {"run_id": state.run_id}
    ledger.config = cfg
    root = cfg["configurable"]["sandbox_root_signing_key"]
    gateway = GatewayRuntime(resources.Configuration.from_runnable_config(cfg))

    async def lookup(run_id):
        return ledger if run_id == state.run_id else None

    def old(run_id):
        raise AssertionError("native request reached legacy authority")

    app = FastAPI()
    app.include_router(
        build_internal_sandbox_router(
            old, native_ledger=lookup, native_root_key=lambda: root
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://api"
    ) as client:
        request = gateway.internal.signed(
            EgressTargetCheckRequest,
            run_id=state.run_id,
            fence_token=lease.fence,
            capability="network",
            target={"domain": "example.org", "port": 443},
        )
        response = await client.post(
            "/internal/sandbox/egress/target/check",
            json=request.model_dump(mode="json"),
        )
        assert response.status_code == 200, response.text
        assert (
            await client.post(
                "/internal/sandbox/egress/target/check",
                json=request.model_dump(mode="json"),
            )
        ).status_code == 401
        request = gateway.internal.signed(
            TeamBridgeRequest,
            run_id=state.run_id,
            fence_token=lease.fence,
            task_id="t",
            action="catalog",
            payload={},
        )
        assert (
            await client.post(
                "/internal/sandbox/team", json=request.model_dump(mode="json")
            )
        ).json() == {"tools": []}
        from open_deep_research.events.task_activity import TaskActivityStore

        activity_request = gateway.internal.signed(
            TaskActivityPublishRequest, run_id=state.run_id, task_id="t",
            fence_token=lease.fence, event_type="model.completed", kind="model",
            phase="reasoning", status="success", title="Model completed",
            payload={"model": "fixture", "input_tokens": 12, "api_key": "do-not-expose"},
            dedupe_key="physical-model-1",
        )
        activity_url = "/internal/sandbox/task-activity"
        response = await client.post(activity_url, json=activity_request.model_dump(mode="json"))
        assert response.status_code == 200 and response.json() == {"published": True}
        assert (await client.post(activity_url, json=activity_request.model_dump(mode="json"))).status_code == 401
        records = TaskActivityStore(state.run_id, "t", runs_dir=str(tmp_path)).read()
        assert len(records) == 1 and records[0].payload["input_tokens"] == 12
        assert "do-not-expose" not in records[0].model_dump_json()
        await store.release(lease)
        stale = gateway.internal.signed(
            TaskActivityPublishRequest, run_id=state.run_id, task_id="t",
            fence_token=lease.fence, event_type="model.completed", dedupe_key="late",
        )
        assert (await client.post(activity_url, json=stale.model_dump(mode="json"))).status_code == 409
        request = gateway.internal.signed(
            EgressTargetCheckRequest,
            run_id=state.run_id,
            fence_token=lease.fence,
            capability="network",
            target={"domain": "example.org", "port": 443},
        )
        assert (
            await client.post(
                "/internal/sandbox/egress/target/check",
                json=request.model_dump(mode="json"),
            )
        ).status_code == 409


async def test_explicit_tool_credentials_override_worker_environment(monkeypatch):
    import json

    import httpx
    from pydantic import BaseModel, SecretStr

    from open_deep_research.sandbox.gateway_tool import GatewayToolProxy
    from open_deep_research.tools.base import ToolContext, ToolOrigin, build_tool

    class Input(BaseModel):
        query: str

    async def forbidden(*args):
        raise AssertionError("host executed gateway tool")

    tool = build_tool(
        name="web_research",
        input_schema=Input,
        description="Web",
        call=forbidden,
        origin=ToolOrigin.SYSTEM,
    )
    seen = []

    def handler(request):
        seen.append((request.url.host, request.headers["authorization"]))
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": "evidence",
                "logical_operation_id": body["logical_operation_id"],
                "tool_call_id": body["tool_call_id"],
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setenv("SANDBOX_TASK_TOKEN", "foreign-token")
    monkeypatch.setenv("SANDBOX_GATEWAY_URL", "http://foreign.invalid")
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)),
    )
    context = ToolContext(
        role="researcher",
        config={"metadata": {"run_id": "r", "task_id": "t"}},
        tool_call_id="c",
    )
    result = await GatewayToolProxy(
        tool, "http://owned.invalid", SecretStr("owned-token")
    ).call(Input(query="q"), context)
    assert result.output == "evidence"
    assert seen == [("owned.invalid", "Bearer owned-token")]
