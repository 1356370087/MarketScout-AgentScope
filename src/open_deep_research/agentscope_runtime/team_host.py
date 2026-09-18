"""Deployment-owned team resources and deterministic run/session binding."""

import asyncio
import json
from uuid import NAMESPACE_URL, uuid5

import asyncpg
from agentscope.agent import ContextConfig, ReActConfig
from agentscope.app.storage import AgentData, AgentRecord, SessionConfig

from open_deep_research.agentscope_runtime.team import (
    FencedTeamTransport,
    NativeResearchTeam,
    research_member_template,
)


class NativeTeamHost:
    """Bind M7 teams to the same PostgreSQL lease and configured MessageBus."""

    def __init__(self, pool, storage, message_bus, *, recovery_schema, owns_pool=False):
        self.pool, self.storage, self.message_bus = pool, storage, message_bus
        self.recovery_schema, self.owns_pool = recovery_schema, owns_pool
        self.delivery = None
        self.delivery_lock = asyncio.Lock()
        self.settings = None

    @classmethod
    async def start(cls, runtime):
        settings = runtime.settings
        if settings.is_demo:
            raise ValueError("durable_team_requires_postgresql")
        if not settings.rocketmq_endpoint:
            raise ValueError("durable_team_requires_configured_remote_rocketmq")
        pool = await asyncpg.create_pool(
            settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1),
            min_size=1,
            max_size=10,
            command_timeout=10,
        )
        try:
            async with pool.acquire() as db:
                await db.fetchval(
                    "SELECT count(*) FROM research_coordination_transactions WHERE false"
                )
            host = cls(
                pool,
                runtime.storage,
                runtime.message_bus,
                recovery_schema=settings.database_schema,
                owns_pool=True,
            )
            host.settings = settings
            return host
        except BaseException:
            await pool.close()
            raise

    async def bind(self, recovery, *, max_iters=10):
        lease = recovery.lease
        from open_deep_research.agentscope_runtime.run_config import RunConfig
        frozen = getattr(getattr(recovery, "snapshot", None), "application", {}).get("configuration")
        teams = bool(frozen and RunConfig.restore(frozen).get("async_research_mode") == "teams")
        if teams and self.settings is not None:
            async with self.delivery_lock:
                if self.delivery is None:
                    from open_deep_research.tasks.team_delivery import TeamDelivery
                    delivery = TeamDelivery(self.pool, self.settings)
                    await delivery.start()
                    self.delivery = delivery

        def identity(kind):
            return uuid5(
                NAMESPACE_URL, json.dumps([lease.user_id, lease.run_id, kind])
            ).hex

        agent_id, session_id = identity("leader-agent"), identity("leader-session")
        # Check the live database fence before writing native projections.
        transport = FencedTeamTransport(
            self.pool,
            lease,
            recovery_schema=self.recovery_schema,
            message_bus=self.message_bus,
            reliable=teams,
        )
        from open_deep_research.agentscope_runtime.native_security import NativeEventPublisher
        if hasattr(recovery, "store"):
            transport.publisher = NativeEventPublisher(recovery.store, recovery.lease)
        async with self.pool.acquire() as db, db.transaction():
            await transport.guard(db)
        existing = await self.storage.get_agent(lease.user_id, agent_id)
        if existing is None:
            await self.storage.upsert_agent(
                lease.user_id,
                AgentRecord(
                    id=agent_id,
                    user_id=lease.user_id,
                    data=AgentData(
                        name="lead",
                        context_config=ContextConfig(),
                        react_config=ReActConfig(),
                    ),
                ),
            )
        session = await self.storage.get_session(lease.user_id, agent_id, session_id)
        if session is None:
            await self.storage.upsert_session(
                lease.user_id,
                agent_id,
                SessionConfig(name="leader", workspace_id=lease.run_id),
                session_id=session_id,
            )
        team = NativeResearchTeam(
            self.storage,
            transport,
            leader_agent_id=agent_id,
            leader_session_id=session_id,
            template=research_member_template(max_iters=max_iters),
        )
        if not teams:
            await team.create("research", "Evidence-based research")
        return team

    async def aclose(self):
        if self.delivery:
            await self.delivery.aclose()
        if self.owns_pool:
            await self.pool.close()
