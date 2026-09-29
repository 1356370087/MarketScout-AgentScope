"""RESEARCH_ENGINE=native 的宿主组合：新研究运行改走原生运行时。

本模块是旧执行路径的退出缝：默认（legacy）不导入、不改变任何旧行为；
显式设置 ``RESEARCH_ENGINE=native`` 时，研究运行族路由到 ``NativeRuns``
（SQL 恢复权威、持久决策、原生事件投影），旧引擎仅继续服务其历史运行。

边界（诚实声明）：
- 宿主直连提供商模型（sandbox_enabled=false）与宿主区工具可用；
- AS_NATIVE_RESOURCES=gateway 显式接入生产资源提供器（Run Key、Gateway
  注册与任务代理），要求 PostgreSQL；host 模式仍拒绝网关区工具；
- 可信团队 Worker 不执行沙箱本地 shell/file 工具；完整出网审批需联合验收；
- 默认入口切换属 T080 切换演练决策，本缝只提供机制。
"""

from __future__ import annotations

import os
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from open_deep_research.agentscope_runtime.production import (
    ProductionRunFactory,
    RunResources,
    authorize_run_owner,
)
from open_deep_research.tools.base import ToolExecutionZone

LEGACY_ENGINE = "legacy"
NATIVE_ENGINE = "native"


def research_engine() -> str:
    """部署级引擎选择；默认保持旧引擎，切换由 T080 切换演练决策。"""
    value = os.environ.get("RESEARCH_ENGINE", LEGACY_ENGINE).strip().lower()
    if value not in {LEGACY_ENGINE, NATIVE_ENGINE}:
        raise RuntimeError(f"unsupported RESEARCH_ENGINE: {value}")
    return value


def native_engine_enabled() -> bool:
    return research_engine() == NATIVE_ENGINE


def mount_native_research(app, service) -> None:
    """RESEARCH_ENGINE=native：原生研究路由置于旧路由之前（切换模式语义）。

    先注册者优先匹配：/runs 创建、生命周期、审批、SSE、团队与发布路由由
    原生运行时服务；旧引擎的历史运行退为只读归档视图（恢复/审批返回
    409 legacy_checkpoint_read_only）。该模式要求旧在途运行已排空或接受
    只读化，是 T080 切换演练的部署形态。
    """
    from open_deep_research.api.research_router import build_research_router

    before = len(app.router.routes)
    app.include_router(build_research_router(service))
    mounted = app.router.routes[before:]
    app.router.routes[:] = mounted + app.router.routes[:before]


def _host_local_zones() -> frozenset[ToolExecutionZone]:
    """宿主直执行区：HOST_CONTROL 与本地文件/知识库工具的 SANDBOX_LOCAL。

    与旧引擎免沙箱部署同信任级；网关区工具不经本缝主机执行。
    """
    return frozenset({ToolExecutionZone.HOST_CONTROL, ToolExecutionZone.SANDBOX_LOCAL})


async def _host_tools_for(role, config):
    from open_deep_research.tools.read_file import read_file
    from open_deep_research.tools.search_documents import search_documents
    from open_deep_research.tools.shell_exec import shell_exec
    from open_deep_research.tools.write_file import write_file

    tools = [search_documents, read_file, write_file, shell_exec]
    return [tool for tool in tools if tool.is_enabled(config)]


def _host_resources(runs_dir: Path):
    """宿主资源提供器：直连提供商凭据绑定与宿主区工具，本地 SQL 计账。"""

    @asynccontextmanager
    async def open_resources(run_config, config, recovery):
        from open_deep_research.agentscope_runtime.models import (
            ROLES,
            ModelFactory,
            bind_role,
        )
        from open_deep_research.configuration import Configuration

        cfg = Configuration.from_runnable_config(config)
        if cfg.sandbox_enabled:
            raise ValueError(
                "native_host_requires_gateway_resources: sandbox runs need the "
                "M10 production resource provider"
            )
        if cfg.search_api != "none" or cfg.web_pipeline_mode != "legacy":
            raise ValueError(
                "native_host_requires_gateway_resources: web research tools "
                "execute in the sandbox gateway; configure RESEARCH_ENGINE=legacy "
                "until the resource provider is deployed"
            )
        owner = recovery.lease.user_id
        run_id = recovery.lease.run_id
        bindings = {}
        for role in ROLES:
            try:
                bindings[role] = bind_role(
                    run_config,
                    role,
                    reference=f"run:{run_id}:{role}",
                    scope="run",
                    owner=owner,
                    source=config.get("metadata"),
                )
            except ValueError:
                # 未配置的可选角色不绑定；用到时由工厂的描述符检查拒绝。
                continue
        factory = ModelFactory(run_config, scope="run", owner=owner, bindings=bindings)
        try:
            yield RunResources(
                models=factory,
                tools_for=_host_tools_for,
                local_zones=_host_local_zones(),
            )
        finally:
            await factory.aclose()

    return open_resources


async def build_native_research_service(*, runs_dir, database_url=None, resource_provider=None, external_workers=True):
    """组装 NativeRuns 服务；恢复权威建表，工厂持有宿主资源生命周期。"""
    from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
    from open_deep_research.api.native_runs import NativeRuns

    runs_dir = Path(runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    if database_url is None:
        database_url = "sqlite+aiosqlite:///" + (runs_dir / "native-recovery.db").as_posix()
    runtime = None
    production = resource_provider is None and os.environ.get("AS_NATIVE_RESOURCES", "host") == "gateway"
    if production:
        from open_deep_research.agentscope_runtime.app import ASRuntime
        from open_deep_research.agentscope_runtime.production_resources import (
            prepare_production_config, production_resources,
        )
        from open_deep_research.sandbox.team_controller import ControllerTeamLauncher
        from open_deep_research.sandbox.controller_client import SandboxControllerClient
        from open_deep_research.configuration import Configuration
        from open_deep_research.sandbox.schema import resolve_profile

        runtime = await ASRuntime.create()
        try:
            if runtime.settings.is_demo:
                raise ValueError("production native resources require AS_DATABASE_URL")
            store = await runtime.create_recovery_store()
        except BaseException:
            await runtime.aclose()
            raise

        def launcher_factory():
            cfg = Configuration.from_runnable_config(None)
            bundle, _, _ = resolve_profile(cfg)
            return ControllerTeamLauncher(SandboxControllerClient(cfg, bundle))

        factory = ProductionRunFactory(runtime, authorize_run_owner,
            production_resources(runs_dir, launcher_factory=launcher_factory if external_workers else None), runs_dir=runs_dir)
        prepare_config = prepare_production_config
    else:
        store = RecoveryStore(database_url)
        await store.create_tables()
        factory = ProductionRunFactory(
            None,
            authorize_run_owner,
            resource_provider or _host_resources(runs_dir),
            runs_dir=runs_dir,
        )

        async def prepare_config(request, principal):
            return {
                "configurable": request.configurable,
                "metadata": {
                    "user_id": principal.user_id,
                    "langgraph_auth_user": {
                        "identity": principal.user_id,
                        "permissions": sorted(principal.permissions),
                    },
                },
            }

    service = NativeRuns(store, factory, prepare_config, runs_dir=runs_dir)

    reconciliation = None
    if production:
        from open_deep_research.agentscope_runtime.spend_reconciliation import reconciliation_loop
        reconciliation = asyncio.create_task(reconciliation_loop(service))

    async def aclose():
        if reconciliation is not None:
            reconciliation.cancel()
            await asyncio.gather(reconciliation, return_exceptions=True)
        await service.aclose()
        if runtime is not None:
            await runtime.aclose()
        else:
            await store.aclose()

    service.native_aclose = aclose
    return service
