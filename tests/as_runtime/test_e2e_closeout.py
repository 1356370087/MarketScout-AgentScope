"""Regression checks for production E2E boundaries, using actual SQL receipts."""

# ruff: noqa: F811 -- imported pytest fixture

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from test_recovery import create, store  # noqa: F401

from open_deep_research.agentscope_runtime.native_security import (
    NativeEventPublisher,
    cleanup_run_key,
    sandbox_context,
)
from open_deep_research.agentscope_runtime.recovery_events import public_events
from open_deep_research.agentscope_runtime.run_config import RunConfig
from open_deep_research.agentscope_runtime.spend_reconciliation import reconcile_spend

pytestmark = pytest.mark.asyncio


async def test_production_context_offload_preserves_history_and_rejects_stale_owner(store, tmp_path):
    import json
    from agentscope.message import UserMsg
    from open_deep_research.agentscope_runtime.context import ResearchContextMiddleware, RunContextOffloader
    from open_deep_research.agentscope_runtime.recovery_store import FenceLost

    state, lease = await create(store)
    offloader = RunContextOffloader(tmp_path, SimpleNamespace(store=store, lease=lease))
    messages = [UserMsg("user", "question"), UserMsg("user", "x" * 10000), UserMsg("user", "continue")]
    agent = SimpleNamespace(state=SimpleNamespace(context=messages, session_id="session", middle_context={}))
    await ResearchContextMiddleware(max_chars=2000, offloader=offloader).on_compress_context(agent, {}, None)
    files = list(offloader.directory.glob("*.json"))
    assert len(files) == 1
    assert len(json.loads(files[0].read_text(encoding="utf-8"))["messages"]) == 3
    assert len(agent.state.context) == 2
    assert agent.state.middle_context["research_context_refs"] == [f"run-context://{state.run_id}/{files[0].name}"]
    await store.release(lease)
    with pytest.raises(FenceLost):
        await offloader.offload_context("session", messages)
    assert len(list(files[0].parent.glob("*.json"))) == 1


async def test_creation_freezes_document_generation_and_default_proxy_budget(
    monkeypatch,
):
    from open_deep_research.agentscope_runtime import production_resources as resources
    from open_deep_research.api.contracts import RunRequest
    from open_deep_research.documents import database, repository
    from tests.auth_helpers import research_principal

    async def validate(owner, selection):
        return [
            {
                "id": "doc",
                "filename": "a.txt",
                "sha256": "digest",
                "current_generation_id": "generation-1",
            }
        ]

    class Catalog:
        def __init__(self, **kwargs):
            pass

        async def load(self):
            return {
                "fixture": resources.ModelCatalogEntry(
                    model_name="fixture",
                    context_window=32000,
                    max_output_tokens=2000,
                    input_cost_per_token=0.001,
                    output_cost_per_token=0.002,
                )
            }

        async def aclose(self):
            pass

    monkeypatch.setattr(database, "document_schema_available", lambda: True)
    monkeypatch.setattr(repository, "validate_selection", validate)
    monkeypatch.setattr(resources, "allowed_models", lambda cfg: ["fixture"])
    monkeypatch.setattr(resources, "LiteLLMModelCatalogClient", Catalog)
    monkeypatch.setattr(
        resources.RunKeySettings,
        "from_env",
        lambda: SimpleNamespace(
            base_url="http://proxy/v1",
            master_key="fixture",
            resolve_budget=lambda requested: 1234,
        ),
    )
    request = RunRequest(
        messages=[{"role": "user", "content": "q"}],
        configurable={"model_backend": "litellm"},
        source_selection={
            "mode": "documents",
            "sources": [{"type": "document", "id": "doc"}],
        },
    )
    config = await resources.prepare_production_config(
        request, research_principal("alice")
    )
    assert config["configurable"]["max_run_cost_micro_usd"] == 1234
    assert (
        config["metadata"]["selected_source_snapshots"][0]["current_generation_id"]
        == "generation-1"
    )


async def test_document_selection_is_authorized_before_opening_model_credentials(
    monkeypatch,
):
    from open_deep_research.agentscope_runtime.production_resources import (
        prepare_production_config,
    )
    from open_deep_research.api.contracts import RunRequest
    from open_deep_research.documents import database, repository
    from tests.auth_helpers import research_principal

    seen = []

    async def validate(owner, selection):
        seen.append(owner)
        raise KeyError("foreign document")

    monkeypatch.setattr(database, "document_schema_available", lambda: True)
    monkeypatch.setattr(repository, "validate_selection", validate)
    request = RunRequest(
        messages=[{"role": "user", "content": "q"}],
        configurable={"model_backend": "litellm"},
        source_selection={
            "mode": "documents",
            "sources": [{"type": "document", "id": "foreign"}],
        },
    )
    with pytest.raises(HTTPException) as error:
        await prepare_production_config(request, research_principal("alice"))
    assert error.value.status_code == 404 and seen == ["alice"]


async def test_native_security_resolves_sql_epoch_and_rejects_released_lease(
    store, tmp_path
):
    state, lease = await create(store)
    state.application["configuration"] = RunConfig.compile(
        {"configurable": {}}
    ).snapshot()
    await store.save(lease, state)
    service = SimpleNamespace(store=store, runs_dir=tmp_path)
    cfg, fence, config = await sandbox_context(
        service, state.run_id, require_live_fence=True
    )
    assert fence == lease.fence and cfg.runs_dir == str(tmp_path)
    assert config["metadata"]["owner"] == "owner"
    await store.release(lease)
    with pytest.raises(HTTPException) as error:
        await sandbox_context(service, state.run_id, require_live_fence=True)
    assert error.value.status_code == 409
    assert await sandbox_context(service, "missing") is None


async def test_cleanup_preserves_live_native_key_and_cleans_abandoned_key(store):
    state, lease = await create(store)
    calls = []

    async def finalize(run_id):
        calls.append(run_id)
        return True

    manager = SimpleNamespace(finalize=finalize)
    assert await cleanup_run_key(store, state.run_id, manager)
    assert not calls
    await store.release(lease)
    assert await cleanup_run_key(store, state.run_id, manager)
    assert calls == [state.run_id]


async def test_native_approval_events_are_durable_sanitized_and_deduplicated(store):
    state, lease = await create(store)
    publisher = NativeEventPublisher(store, lease)
    for _ in range(2):
        await publisher.publish(
            "security.approval.required",
            payload={
                "approval_id": "a",
                "kind": "egress",
                "api_key": "secret",
            },
            dedupe_key="approval:a",
        )
    events = await store.events(state.run_id, "owner")
    projected = [item for event in events for item in public_events(event)]
    assert len(projected) == 1
    assert projected[0][0] == "security.approval.required"
    assert "secret" not in str(projected)


async def test_proxy_bill_exact_join_idempotent_delta_and_receipt_replay(store):
    state, lease = await create(store)
    key = "gateway:model:op"
    await store.begin_operation(
        lease,
        key,
        "gateway:model",
        {},
        reserve={"model_calls": 1, "input_tokens": 10, "cost_micro_usd": 100},
    )
    receipt = {"status": "completed", "outcome": {}}
    await store.commit_operation(lease, key, receipt)
    log = {
        "request_id": "bill",
        "request_tags": [f"run:{state.run_id}", "operation:op"],
        "prompt_tokens": 4,
        "completion_tokens": 2,
        "spend": "0.000030",
    }
    for _ in range(2):
        result = await reconcile_spend(store, lease, [log, log])
        assert result["corrected"] == [key]
    await store.commit_operation(lease, key, receipt)
    budget = await store.budget(state.run_id, "owner")
    assert budget["used"] == {
        "model_calls": 1,
        "input_tokens": 4,
        "output_tokens": 2,
        "cost_micro_usd": 30,
    }


async def test_proxy_bill_does_not_resolve_unknown_execution_or_accept_other_run(store):
    _state, lease = await create(store)
    await store.begin_operation(
        lease, "gateway:model:op", "gateway:model", {}, reserve={"model_calls": 1}
    )
    result = await reconcile_spend(
        store,
        lease,
        [{"request_id": "other", "request_tags": ["run:another", "operation:op"]}],
    )
    assert result == {"corrected": [], "unresolved": ["gateway:model:op"]}
    assert (await store.operation_record(lease, "gateway:model:op"))[
        "state"
    ] == "started"


async def test_background_billing_does_not_acquire_waiting_run(store, monkeypatch):
    import asyncio

    from open_deep_research.agentscope_runtime import spend_reconciliation as billing

    run_ids = {}
    for status in ("waiting", "completed"):
        state, lease = await create(store)
        state.status = status
        await store.save(lease, state)
        await store.begin_operation(lease, "model", "gateway:model", {})
        await store.commit_operation(lease, "model", {"status": "completed"})
        await store.release(lease)
        run_ids[status] = state.run_id

    calls = []
    cycles = 0

    async def sleep(_interval):
        nonlocal cycles
        cycles += 1
        if cycles > 1:
            raise asyncio.CancelledError

    async def collect(_store, lease):
        calls.append(lease.run_id)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr(billing, "collect_spend", collect)
    with pytest.raises(asyncio.CancelledError):
        await billing.reconciliation_loop(SimpleNamespace(store=store))
    assert calls == [run_ids["completed"]]


async def test_supervisor_task_lifecycle_reaches_public_projection(store):
    from agentscope.message import TextBlock
    from test_research_migration import Models, cfg, contract, tool_call

    from open_deep_research.agentscope_runtime.recovery import RecoverySession
    from open_deep_research.agentscope_runtime.research_agents import ResearchHandoff, Supervisor
    from open_deep_research.events.public import PublicEvent, project_public_events

    state, lease = await create(store)
    models = Models({"supervisor": [
        [tool_call("TaskCreate", "one", research_topic="official evidence")],
        [TextBlock(text="done")],
    ]})
    models.recovery = RecoverySession(store, lease, state)

    class Worker:
        async def run(self, assignment, contract, feedback):
            return ResearchHandoff(**assignment.model_dump(), compressed_research="done")

    try:
        results, _ = await Supervisor(
            models, lambda: cfg(enable_async_research=True), Worker(), run_id=state.run_id,
        ).run("brief", contract())
        events = [mapped for event in await store.events(state.run_id, "owner")
                  for mapped in public_events(event) if mapped[0].startswith("research.task.")]
        assert [item[0] for item in events] == [
            "research.task.created", "research.task.started", "research.task.completed",
        ]
        projected = project_public_events([
            PublicEvent(run_id=state.run_id, event_id=str(i), sequence=i + 1,
                        timestamp="2026-09-18T00:00:00Z", type=kind, stage=stage,
                        payload=payload, dedupe_key=str(i))
            for i, (kind, stage, payload) in enumerate(events)
        ])
        task = projected.task_items[results[0]["task_id"]]
        assert task["status"] == "completed"
        assert task["title"] == "official evidence"
    finally:
        await models.recovery.close()


async def test_controller_team_boundary_signatures_fixed_command_and_cleanup(
    monkeypatch, tmp_path
):
    import base64
    from types import MethodType

    import httpx
    from docker.errors import NotFound
    from fastapi import FastAPI

    from open_deep_research.agentscope_runtime.recovery_store import RunLease
    from open_deep_research.sandbox.controller import DockerControllerRuntime
    from open_deep_research.sandbox.crypto import NonceReplayCache, SandboxDerivedKeys
    from open_deep_research.sandbox.team_controller import (
        ControllerTeamLauncher,
        install_team_routes,
    )

    created = []
    containers = {}

    def get(name):
        if name not in containers:
            raise NotFound("missing")
        return containers[name]

    def run(image, command, **kwargs):
        created.append((image, command, kwargs))
        item = SimpleNamespace(labels=kwargs["labels"], status="running")
        item.remove = lambda **options: containers.pop(kwargs["name"])
        containers[kwargs["name"]] = item
        return item

    runtime = SimpleNamespace(
        bundle=SimpleNamespace(deployment_id="test"),
        keys=SandboxDerivedKeys.from_root(base64.b64encode(b"k" * 32).decode()),
        nonces=NonceReplayCache(),
        client=SimpleNamespace(containers=SimpleNamespace(get=get, run=run)),
    )
    runtime._authorize_service = MethodType(
        DockerControllerRuntime._authorize_service, runtime
    )
    env = tmp_path / "worker.env"
    env.write_text("RUNS_DIR=/data/runs\n", encoding="utf-8")
    monkeypatch.setenv("AS_TEAM_WORKER_ENV_FILE", str(env))
    monkeypatch.setenv("AS_TEAM_WORKER_IMAGE", "trusted:local")
    monkeypatch.setenv("AS_TEAM_CONTROLLER_MOUNTS", "[]")
    app = FastAPI()
    install_team_routes(app, runtime)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://controller"
    ) as http:

        async def post(path, request):
            response = await http.post(path, json=request.model_dump(mode="json"))
            response.raise_for_status()
            replay = await http.post(path, json=request.model_dump(mode="json"))
            assert replay.status_code == 422
            tampered = request.model_dump(mode="json")
            tampered["task_id"] = "foreign"
            assert (await http.post(path, json=tampered)).status_code == 422
            return response.json()

        launcher = ControllerTeamLauncher(
            SimpleNamespace(
                bundle=runtime.bundle,
                keys=runtime.keys,
                _post=post,
                transport=http,
            )
        )
        lease = RunLease("run", "user", "owner", 1)
        await launcher.ensure_started(lease, "task")
        await launcher.ensure_started(lease, "task")
        assert len(created) == 1
        assert created[0][1] == [
            "python",
            "-m",
            "open_deep_research.agentscope_runtime.team_executor",
        ]
        assert created[0][2]["cap_drop"] == ["ALL"]
        assert created[0][2]["auto_remove"] is False
        assert created[0][2]["healthcheck"] == {"test": ["NONE"]}
        await launcher.aclose()
        assert not containers
