"""RESEARCH_ENGINE=native 的宿主组合：新研究运行改走原生运行时。

本模块是旧执行路径的退出缝：默认（legacy）不导入、不改变任何旧行为；
显式设置 ``RESEARCH_ENGINE=native`` 时，研究运行族路由到 ``NativeRuns``
（SQL 恢复权威、持久决策、原生事件投影），旧引擎仅继续服务其历史运行。

边界（诚实声明）：
- 宿主直连提供商模型（sandbox_enabled=false）与宿主区工具可用；
- 沙箱运行与网关区工具（web 搜索/抓取）仍需 M10 生产资源提供器
  （LiteLLM Run Key、Gateway 注册与工具代理），本组合对这类配置显式拒绝，
  不做静默降级；
- 默认入口切换属 T080 切换演练决策，本缝只提供机制。
"""

from __future__ import annotations

import os
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


async def build_native_research_service(*, runs_dir, database_url=None):
    """组装 NativeRuns 服务；恢复权威建表，工厂持有宿主资源生命周期。"""
    from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
    from open_deep_research.api.native_runs import NativeRuns

    runs_dir = Path(runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    if database_url is None:
        database_url = "sqlite+aiosqlite:///" + (runs_dir / "native-recovery.db").as_posix()
    store = RecoveryStore(database_url)
    await store.create_tables()
    factory = ProductionRunFactory(
        None,
        authorize_run_owner,
        _host_resources(runs_dir),
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

    async def aclose():
        await service.aclose()
        await store.aclose()

    service.native_aclose = aclose
    return service
