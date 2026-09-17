"""旧路径退出缝：引擎路由、内部预算分派与宿主组合边界。"""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from open_deep_research.agentscope_runtime.native_host import (
    LEGACY_ENGINE,
    NATIVE_ENGINE,
    mount_native_research,
    research_engine,
)
from open_deep_research.agentscope_runtime.recovery import RecoverySession
from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
from open_deep_research.agentscope_runtime.research_pipeline import (
    PendingDecision,
    ResearchPipeline,
    ResearchSnapshot,
)
from open_deep_research.api.native_runs import NativeRuns
from open_deep_research.configuration import Configuration
from open_deep_research.sandbox.gateway import GatewayRuntime
from open_deep_research.sandbox.internal_api import (
    ToolBudgetReserveRequest,
    build_internal_sandbox_router,
)
from agentscope.message import UserMsg
from security.rbac.dependencies import get_current_principal
from tests.auth_helpers import research_principal

pytestmark = pytest.mark.asyncio


async def test_engine_flag_defaults_to_legacy_and_validates(monkeypatch):
    monkeypatch.delenv("RESEARCH_ENGINE", raising=False)
    assert research_engine() == LEGACY_ENGINE
    monkeypatch.setenv("RESEARCH_ENGINE", "native")
    assert research_engine() == NATIVE_ENGINE
    monkeypatch.setenv("RESEARCH_ENGINE", "quantum")
    with pytest.raises(RuntimeError, match="unsupported RESEARCH_ENGINE"):
        research_engine()


@pytest_asyncio.fixture
async def native_service(tmp_path):
    store = RecoveryStore("sqlite+aiosqlite:///" + (tmp_path / "seam.db").as_posix())
    await store.create_tables()

    class Stages:
        async def execute(self, stage, state):
            if stage == "plan_approval":
                return PendingDecision(stage=stage, question="Confirm plan")
            if stage == "final_report_generation":
                state.final_report = "Native report"

    @asynccontextmanager
    async def factory(state, config, recovery: RecoverySession):
        try:
            yield ResearchPipeline(
                state,
                Stages(),
                recovery.save,
                config_fingerprint=state.config_fingerprint,
                recovery=recovery,
            )
        finally:
            pass

    async def prepare(request, principal):
        return {
            "configurable": request.configurable,
            "metadata": {"user_id": principal.user_id},
        }

    service = NativeRuns(store, factory, prepare, runs_dir=tmp_path / "archive")
    yield service
    await service.aclose()
    await store.aclose()


async def test_native_mount_takes_precedence_over_legacy_run_routes(native_service):
    """切换模式下原生路由优先：同一 /runs 路径由原生服务接管的机制验证。"""
    app = FastAPI()

    @app.post("/runs")
    async def legacy_create():
        return JSONResponse({"run_id": "legacy", "events_url": "/runs/legacy/events"})

    mount_native_research(app, native_service)
    app.dependency_overrides[get_current_principal] = lambda: research_principal(
        "alice"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/runs",
            json={"messages": [{"role": "user", "content": "q"}]},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["run_id"] != "legacy"
        snapshot = await native_service.snapshot(body["run_id"], "alice")
        assert snapshot["engine"] == "agentscope"
    for task in list(native_service.tasks.values()):
        await asyncio.wait_for(asyncio.shield(task), 10)


async def test_internal_budget_dispatches_by_run_engine():
    """原生运行的工具预算进 SQL 权威；旧运行保持旧权威解析路径。"""

    class Ledger:
        def __init__(self):
            self.reserved = []
            self.recovery = SimpleNamespace(lease=SimpleNamespace(fence=1), store=self)

        @asynccontextmanager
        async def transaction(self, lease):
            yield

        async def reserve_tool(self, request):
            self.reserved.append(request.logical_operation_id)
            return {"replayed": False}

    ledger = Ledger()
    legacy_authority_calls = []

    def resolve_run(run_id):
        legacy_authority_calls.append(run_id)
        return None  # 旧权威解析失败将 404；本测试只验证分派顺序。

    async def native_ledger(run_id):
        return ledger if run_id == "native-run" else None

    app = FastAPI()
    app.include_router(
        build_internal_sandbox_router(
            resolve_run, native_ledger=native_ledger,
            native_root_key=lambda: base64.b64encode(b"x" * 32).decode(),
        )
    )
    gateway = GatewayRuntime(
        Configuration(
            sandbox_root_signing_key=base64.b64encode(b"x" * 32).decode()
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        native_request = gateway.internal.signed(
            ToolBudgetReserveRequest,
            run_id="native-run",
            task_id="t",
            fence_token=1,
            stage="researching",
            logical_operation_id="op-native",
            tool_name="web_research",
            idempotent=True,
        )
        response = await client.post(
            "/internal/sandbox/budgets/tool-reserve",
            json=native_request.model_dump(mode="json"),
        )
        assert response.status_code == 200, response.text
        assert ledger.reserved == ["op-native"]
        assert legacy_authority_calls == []

        # 原生分支也必须拒绝重放和被篡改的签名，不能因分派提前返回绕过校验。
        repeated = await client.post(
            "/internal/sandbox/budgets/tool-reserve",
            json=native_request.model_dump(mode="json"),
        )
        assert repeated.status_code == 401
        invalid = native_request.model_dump(mode="json")
        invalid["service_signature"] = "invalid"
        denied = await client.post("/internal/sandbox/budgets/tool-reserve", json=invalid)
        assert denied.status_code == 401
        assert ledger.reserved == ["op-native"]

        legacy_request = gateway.internal.signed(
            ToolBudgetReserveRequest,
            run_id="legacy-run",
            task_id="t",
            fence_token=1,
            stage="researching",
            logical_operation_id="op-legacy",
        )
        response = await client.post(
            "/internal/sandbox/budgets/tool-reserve",
            json=legacy_request.model_dump(mode="json"),
        )
        # 旧运行走旧权威解析（此处解析失败 404），不触碰 SQL 账本。
        assert response.status_code == 404
        assert legacy_authority_calls == ["legacy-run"]
        assert ledger.reserved == ["op-native"]


async def test_host_resources_refuse_gateway_requiring_configs(tmp_path):
    """宿主组合对沙箱/网关工具配置显式拒绝，不做静默降级。"""
    from open_deep_research.agentscope_runtime.native_host import _host_resources
    from open_deep_research.agentscope_runtime.run_config import RunConfig

    run = RunConfig.compile(
        {"configurable": {"sandbox_enabled": False, "search_api": "tavily"}}
    )
    resources = _host_resources(tmp_path)
    lease = SimpleNamespace(run_id="r", user_id="alice")
    with pytest.raises(ValueError, match="native_host_requires_gateway_resources"):
        async with resources(run, {"configurable": {}, "metadata": {}}, lease):
            pass


async def test_native_host_composition_builds_durable_service(tmp_path):
    from open_deep_research.agentscope_runtime.native_host import (
        build_native_research_service,
    )

    service = await build_native_research_service(
        runs_dir=tmp_path / "runs", database_url=None
    )
    try:
        # 恢复权威表已建：能直接创建并读回运行。
        await service.store.create_run(
            "alice",
            ResearchSnapshot(
                run_id="compose-check",
                config_fingerprint="f",
                messages=[UserMsg("user", "q")],
            ),
        )
        state, _ = await service.store.load("compose-check", "alice")
        assert state.run_id == "compose-check"
    finally:
        await service.native_aclose()
