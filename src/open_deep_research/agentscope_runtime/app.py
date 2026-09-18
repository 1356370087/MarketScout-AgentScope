"""AgentScope 应用组合入口（M2/AS-T010）。

把框架服务组件装配为可在现有进程内挂载的 FastAPI 子应用：

- 存储：``build_storage``（PG 独立 schema / 演示 SQLite）。
- 总线：``AS_DATABASE_URL`` 存在时用持久 ``PostgreSQLMessageBus``，
  演示模式退回 ``InMemoryMessageBus``（锁 TTL 缺口 AS-R021 已知）。
- 身份：安装 IAM JWT 覆盖（AS-T015），伪造 ``X-User-ID`` 无效。
- 暴露策略：子应用默认**不挂载到对外路由**（最小暴露，AS-T015）；
  兼容 API 外壳由旧 ``server:app`` 继续承担（M11 前不变），本入口供
  进程内服务组合与测试使用。

用法（进程内组合）::

    runtime = await ASRuntime.create()      # 打开存储与总线
    app = runtime.build_app()               # 框架子应用（已覆盖身份）
    try:
        ...                                 # ASGI 挂载 / TestClient
    finally:
        await runtime.aclose()              # T016 顺序：先停接收再释放
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from open_deep_research.agentscope_runtime.lifecycle import (
    AdmissionMiddleware,
    BorrowedResource,
    ShutdownGate,
    ShutdownStack,
    run_shutdown_sequence,
)
from open_deep_research.agentscope_runtime.settings import ASRuntimeSettings
from open_deep_research.agentscope_runtime.storage import build_storage


@dataclass
class ASRuntime:
    """持有框架服务组件的组合根；生命周期由调用方显式管理（AS-T016）。"""

    settings: ASRuntimeSettings
    storage: Any
    message_bus: Any
    workspace_manager: Any | None = None
    _owns: list[str] = field(default_factory=list)
    gate: ShutdownGate = field(default_factory=ShutdownGate)
    shutdown_stack: ShutdownStack = field(default_factory=ShutdownStack)
    commands: Any | None = None
    broadcast: Any | None = None
    _close_task: Any | None = None
    _team_host: Any | None = None
    _team_start_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @classmethod
    async def create(cls, settings: ASRuntimeSettings | None = None) -> ASRuntime:
        settings = settings or ASRuntimeSettings.from_env()
        storage = build_storage(settings)
        runtime = cls(settings=settings, storage=storage, message_bus=None)
        runtime.shutdown_stack.push_base(
            "storage", lambda: storage.__aexit__(None, None, None)
        )
        try:
            await storage.__aenter__()
            await runtime._start_bus()
        except BaseException:
            await runtime.aclose()
            raise
        return runtime

    async def _start_bus(self) -> None:
        settings = self.settings
        if settings.is_demo:
            from agentscope.app.message_bus import InMemoryMessageBus

            bus = InMemoryMessageBus()
        else:
            from open_deep_research.agentscope_runtime.pgbus import PostgreSQLMessageBus
            from open_deep_research.agentscope_runtime.storage import (
                build_engine_kwargs,
            )

            bus = PostgreSQLMessageBus(
                settings.database_url,
                table_prefix=settings.bus_table_prefix,
                engine_kwargs=build_engine_kwargs(settings),
                auto_create=settings.storage_auto_create,
            )
            self.shutdown_stack.push_base("message_bus", bus.aclose)
            await bus.__aenter__()
            from open_deep_research.agentscope_runtime.durable import (
                DurableCommandBridge,
            )

            self.commands = DurableCommandBridge(
                settings.database_url,
                table_prefix=settings.bus_table_prefix,
                engine_kwargs=build_engine_kwargs(settings),
                signal_queue=bus,
                auto_create=settings.storage_auto_create,
                gate=self.gate,
            )
            self.shutdown_stack.push_base("commands", lambda: self.commands.__aexit__())
            await self.commands.__aenter__()
            if settings.rocketmq_endpoint:
                if not settings.rocketmq_group:
                    raise ValueError(
                        "AS_ROCKETMQ_GROUP must be unique per running instance"
                    )
                from open_deep_research.agentscope_runtime.broadcast import (
                    RocketMQBroadcast,
                )

                self.broadcast = RocketMQBroadcast(
                    settings.rocketmq_endpoint,
                    topic_prefix=settings.rocketmq_topic_prefix,
                    access_key=settings.rocketmq_access_key,
                    secret_key=settings.rocketmq_secret_key,
                    tls=settings.rocketmq_tls,
                )
                self.shutdown_stack.push_base("broadcast", self.broadcast.aclose)
                await self.broadcast.start()
                await self.broadcast.start_consumer(
                    settings.rocketmq_group, channels=["wake"]
                )
                bus.broadcast = self.broadcast
        self.message_bus = bus

    def build_app(self, **create_kwargs: Any):
        """装配框架子应用并安装 IAM 身份覆盖。"""
        from agentscope.app import create_app

        from open_deep_research.agentscope_runtime.identity import (
            install_identity_overrides,
        )

        research_runs = create_kwargs.pop("research_runs", None)
        gateway_ledger_root_key = create_kwargs.pop("gateway_ledger_root_key", None)
        if gateway_ledger_root_key and research_runs is None:
            raise ValueError("gateway ledger requires native research runs")
        kwargs: dict[str, Any] = {
            "storage": BorrowedResource(self.storage),
            "message_bus": BorrowedResource(self.message_bus),
            "enable_channel_worker": False,
            "enable_scheduler": False,
        }
        if self.workspace_manager is not None:
            kwargs["workspace_manager"] = self.workspace_manager
        else:
            from agentscope.app.workspace_manager import LocalWorkspaceManager

            kwargs["workspace_manager"] = LocalWorkspaceManager(
                str(_default_workspace_dir())
            )
        kwargs.update(create_kwargs)
        app = create_app(**kwargs)
        install_identity_overrides(app)
        if research_runs is not None:
            from open_deep_research.api.research_router import build_research_router

            app.include_router(build_research_router(research_runs))
            if gateway_ledger_root_key:
                from open_deep_research.agentscope_runtime.gateway_ledger import (
                    build_gateway_ledger_router,
                )

                app.include_router(build_gateway_ledger_router(
                    research_runs.pipeline_factory.gateway_ledger,
                    gateway_ledger_root_key,
                ))
            self.shutdown_stack.push_drain("native_research_http", research_runs.aclose)
        app.add_middleware(AdmissionMiddleware, gate=self.gate)
        native_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(application):
            context = native_lifespan(application)
            try:
                await context.__aenter__()
                self.shutdown_stack.push_drain(
                    "agentscope_services", lambda: context.__aexit__(None, None, None)
                )
                yield
            finally:
                await self.aclose()

        app.router.lifespan_context = lifespan
        return app

    def knowledge_application(self, user_id, authorize):
        """Bind the authenticated identity and live IAM check to domain operations."""
        from open_deep_research.agentscope_runtime.knowledge import KnowledgeApplication

        if self.gate.closed:
            raise RuntimeError("runtime_shutting_down")
        return KnowledgeApplication(user_id, authorize)

    async def bind_research_team(self, recovery, *, max_iters=10):
        """Bind an authenticated recovery lease to deployment-owned team resources."""
        host = await self.team_host()
        return await host.bind(recovery, max_iters=max_iters)

    async def team_host(self):
        """Open SQL team infrastructure for both active runs and history views."""
        if self.gate.closed:
            raise RuntimeError("runtime_shutting_down")
        async with self._team_start_lock:
            if self._team_host is None:
                from open_deep_research.agentscope_runtime.team_host import (
                    NativeTeamHost,
                )

                self._team_host = await NativeTeamHost.start(self)
                self.shutdown_stack.push_base("native_team_pool", self._team_host.aclose)
        return self._team_host

    def start_command_consumer(
        self, command_key: str, applier: Any, *, poll_seconds: float = 1.0
    ) -> None:
        """定期扫描持久事实，广播全丢也能恢复；每个业务键注册一个消费者。

        applier 必须按 command_id 幂等。非幂等外部操作的结果不明情况
        进入隔离，业务核对/操作账本由 M6 接入。
        """
        if self.gate.closed or self.commands is None:
            raise RuntimeError("durable consumer unavailable")
        stopped = asyncio.Event()

        async def consume():
            while not stopped.is_set() and not self.gate.closed:
                await self.commands.apply_pending(command_key, applier)
                await self.message_bus.queue_drain(f"durable:{command_key}")
                try:
                    await asyncio.wait_for(stopped.wait(), poll_seconds)
                except TimeoutError:
                    pass

        task = asyncio.create_task(consume())

        async def stop():
            stopped.set()
            try:
                await asyncio.wait_for(
                    asyncio.shield(task), self.settings.drain_timeout
                )
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        self.shutdown_stack.push_drain(f"commands:{command_key}", stop)

    def create_model_factory(self, run, *, scope, owner, bindings):
        """装配原生角色模型；客户端生命周期纳入运行时关闭顺序。"""
        from open_deep_research.agentscope_runtime.models import ModelFactory

        if self.gate.closed:
            raise RuntimeError("runtime_shutting_down")
        factory = ModelFactory(run, scope=scope, owner=owner, bindings=bindings)
        self.shutdown_stack.push_drain(f"models:{scope}:{owner}", factory.aclose)
        return factory

    async def create_recovery_store(self):
        """Open the M6 authority in the runtime schema or a separate demo file."""
        from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
        from open_deep_research.agentscope_runtime.storage import build_engine_kwargs

        if self.gate.closed:
            raise RuntimeError("runtime_shutting_down")
        if self.settings.is_demo:
            path = _default_workspace_dir().parent / "agentscope-recovery.db"
            url = "sqlite+aiosqlite:///" + path.as_posix()
        else:
            url = self.settings.database_url
        store = RecoveryStore(url, engine_kwargs=build_engine_kwargs(self.settings))
        self.shutdown_stack.push_base("recovery_store", store.aclose)
        if self.settings.is_demo or self.settings.storage_auto_create:
            await store.create_tables()
        return store

    def research_team(self, recovery, coordination_pool, *, leader_agent_id, leader_session_id, template=None):
        """Bind M7 to the existing business pool and runtime-owned MessageBus.

        The host owns the pool and its published business migrations. This
        adapter adds no connection, schema migration or independent run lease.
        """
        from open_deep_research.agentscope_runtime.team import (
            FencedTeamTransport,
            NativeResearchTeam,
            research_member_template,
        )

        if self.gate.closed:
            raise RuntimeError("runtime_shutting_down")
        if self.settings.is_demo:
            raise ValueError("durable research teams require PostgreSQL")
        return NativeResearchTeam(
            self.storage,
            FencedTeamTransport(
                coordination_pool, recovery.lease,
                recovery_schema=self.settings.database_schema,
                message_bus=self.message_bus,
            ),
            leader_agent_id=leader_agent_id, leader_session_id=leader_session_id,
            template=template or research_member_template(),
        )

    async def submit_research_decision(self, store, *, run_id, user_id, command_id, action_id, payload):
        """Commit before best-effort wakeup; callers must use authenticated identity."""
        state = await store.submit_decision(run_id, user_id, command_id, action_id, payload)
        try:
            await self.message_bus.queue_push("research-decisions:" + run_id, {"run_id": run_id})
        except Exception:  # noqa: BLE001 - durable pending decisions are polled on resume
            logging.getLogger(__name__).warning("Research decision persisted; wakeup delivery failed")
        return state

    def start_team_workers(self, workers):
        """Own an M7 consumer and stop it before closing borrowed SQL resources."""
        if self.gate.closed:
            raise RuntimeError("runtime_shutting_down")
        task = asyncio.create_task(workers.serve())

        async def stop():
            await workers.aclose()
            task.cancel()
            result = await asyncio.gather(task, return_exceptions=True)
            if result and isinstance(result[0], Exception):
                raise result[0]

        self.shutdown_stack.push_drain("team_workers:" + workers.team.lease.run_id, stop)
        return task

    async def cancel_research_run(self, store, *, run_id, user_id, command_id):
        """Revoke the business writer before asking the native service to interrupt."""
        await store.request_cancel(run_id, user_id, command_id)

    def research_middleware_factory(self, resolve_authorized_pipeline):
        """Build the public service extension after application ownership checks.

        Pass the returned factory as ``extra_agent_middlewares`` to ``build_app``.
        The resolver receives the authenticated user and session, not an identity
        supplied in a model message or tool argument.
        """
        from open_deep_research.agentscope_runtime.research_pipeline import (
            ResearchPipelineMiddleware,
        )

        async def factory(user_id, agent_id, session_id, workspace):
            if self.gate.closed:
                raise RuntimeError("runtime_shutting_down")
            pipeline = await resolve_authorized_pipeline(user_id, agent_id, session_id, workspace)
            return [ResearchPipelineMiddleware(pipeline, self.gate)]

        return factory

    async def aclose(self) -> None:
        """先拒绝新工作并保存/释放运行资源，再关闭总线和存储连接池。"""
        if self._close_task is None:
            self.gate.close()
            self._close_task = asyncio.create_task(
                run_shutdown_sequence(
                    self.gate,
                    self.shutdown_stack,
                    drain_timeout=self.settings.drain_timeout,
                )
            )
        # 调用者取消不能打断状态保存和清理；等待同一个关闭任务，避免重复释放。
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            await self._close_task
            raise


def _default_workspace_dir():
    from open_deep_research.agentscope_runtime.storage import runtime_data_dir

    d = runtime_data_dir() / "as-workspaces"
    d.mkdir(parents=True, exist_ok=True)
    return d
