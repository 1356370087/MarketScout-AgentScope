"""Production composition preserves authority, frozen config and resource lifetime."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio
from team_worker_fixture import Factory

from open_deep_research.agentscope_runtime.production import (
    ProductionRunFactory,
    RunResources,
)
from open_deep_research.agentscope_runtime.recovery import RecoverySession
from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
from open_deep_research.agentscope_runtime.run_config import RunConfig
from tests.auth_helpers import research_principal

pytestmark = pytest.mark.asyncio


async def test_factory_reuses_creation_generation_instead_of_current_document(setup, monkeypatch):
    from open_deep_research.documents import repository

    run, recovery, path = setup
    selected = [{"id": "doc", "filename": "source.txt", "sha256": "original", "current_generation_id": "published-at-creation"}]
    recovery.snapshot.application["selected_source_snapshots"] = selected
    bindings = []
    async def bind(run_id, owner, documents):
        bindings.append((run_id, owner, documents))
    monkeypatch.setattr(repository, "bind_run_sources", bind)
    async def authorize(owner, application):
        return research_principal(owner)
    @asynccontextmanager
    async def resources(*args):
        yield RunResources(Factory(), lambda assignment: [])
    factory = ProductionRunFactory(None, authorize, resources, runs_dir=path)
    for _ in range(2):
        async with factory(recovery.snapshot, run, recovery):
            pass
    assert bindings == [("run", "alice", selected)] * 2


@pytest_asyncio.fixture
async def setup(tmp_path):
    run = RunConfig.compile(
        {
            "configurable": {
                "enable_async_research": False,
                "sandbox_enabled": False,
                "enable_memory": False,
            }
        }
    )
    store = RecoveryStore("sqlite+aiosqlite:///" + (tmp_path / "runs.db").as_posix())
    await store.create_tables()
    await store.create_from_config("alice", "run", run)
    recovery = await RecoverySession.open(store, "run", "alice")
    yield run, recovery, tmp_path
    await recovery.close()
    await store.aclose()


async def test_factory_binds_owned_native_pipeline_and_closes_ports(setup):
    from agentscope.message import UserMsg, AssistantMsg
    run, recovery, path = setup
    recovery.snapshot.messages = [UserMsg("user", "Research official policies"), AssistantMsg("assistant", "Untrusted fetched content")]
    observed = []

    async def authorize(owner, application):
        return research_principal(owner)

    @asynccontextmanager
    async def resources(frozen, config, session):
        observed.append(config)
        assert frozen is run and session is recovery
        try:
            yield RunResources(Factory(), lambda assignment: [])
        finally:
            observed.append("closed")

    factory = ProductionRunFactory(
        SimpleNamespace(), authorize, resources, runs_dir=path
    )
    async with factory(recovery.snapshot, run, recovery) as pipeline:
        assert pipeline.recovery is recovery
        assert pipeline.team_workers is None
        assert factory.active["run"]["ledger"] is None
        assert observed[0]["metadata"]["owner"] == "alice"
        assert observed[0]["metadata"]["sandbox_egress_intent"] == "Research official policies"
        assert observed[0]["metadata"]["run_fence_token"] == recovery.lease.fence
        assert observed[0]["configurable"]["langgraph_auth_user"]["identity"] == "alice"
    assert observed[-1] == "closed" and factory.active == {}


async def test_factory_checks_live_identity_before_opening_credentials(setup):
    run, recovery, path = setup

    async def authorize(owner, application):
        return research_principal("foreign")

    def forbidden(*args):
        pytest.fail("credentials opened before authorization")

    factory = ProductionRunFactory(None, authorize, forbidden, runs_dir=path)
    with pytest.raises(PermissionError):
        async with factory(recovery.snapshot, run, recovery):
            pass


async def test_framework_host_mounts_signed_ledger_and_business_routes(setup, monkeypatch):
    import base64

    import httpx

    from open_deep_research.agentscope_runtime.app import ASRuntime
    from open_deep_research.agentscope_runtime.settings import ASRuntimeSettings
    from open_deep_research.api.native_runs import NativeRuns

    _, recovery, path = setup
    monkeypatch.setenv("RUNS_DIR", str(path))
    runtime = await ASRuntime.create(ASRuntimeSettings(None, "unused", True, "test_"))
    factory = ProductionRunFactory(runtime, None, None, runs_dir=path)
    service = NativeRuns(recovery.store, factory, None)
    app = runtime.build_app(
        research_runs=service,
        gateway_ledger_root_key=base64.b64encode(b"x" * 32).decode(),
    )
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://host") as client:
            response = await client.get("/openapi.json")
            assert response.status_code == 200
            paths = response.json()["paths"]
            assert "/internal/sandbox/operations/transition" in paths
            assert "/runs/{run_id}/team/messages" in paths
            assert "/runs/{run_id}/publications" in paths
    finally:
        await runtime.aclose()
