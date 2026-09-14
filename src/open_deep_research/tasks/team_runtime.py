"""Deployment-scoped lifecycle for PostgreSQL and RocketMQ coordination."""

from __future__ import annotations

import asyncio
import os

import asyncpg

from open_deep_research.tasks.rocketmq_transport import RocketMQTransport
from open_deep_research.tasks.team_service import TeamService
from open_deep_research.tasks.team_state import PostgresTaskStateStore
from open_deep_research.tasks.team_store import TeamStore


class TeamRuntime:
    """Own service clients independently of individual research runs."""

    def __init__(self):
        """Initialize without network connections or import-time side effects."""
        self.pool: asyncpg.Pool | None = None
        self.transport: RocketMQTransport | None = None
        self.service: TeamService | None = None
        self.state: PostgresTaskStateStore | None = None
        self.lock = asyncio.Lock()

    async def start(self) -> TeamService:
        """Connect explicitly configured infrastructure and validate migrations."""
        async with self.lock:
            if self.service is not None:
                return self.service
            dsn = os.getenv("COORDINATION_DATABASE_URL", "")
            endpoint = os.getenv("ROCKETMQ_ENDPOINT", "")
            if not dsn or not endpoint:
                raise RuntimeError("COORDINATION_DATABASE_URL and ROCKETMQ_ENDPOINT are required")
            self.pool = await asyncpg.create_pool(
                dsn.replace("postgresql+asyncpg://", "postgresql://", 1),
                min_size=1, max_size=10, command_timeout=10,
            )
            try:
                async with self.pool.acquire() as db:
                    await db.fetchval("SELECT count(*) FROM research_coordination_transactions WHERE false")
                self.transport = RocketMQTransport(
                    TeamStore(self.pool), endpoint=endpoint,
                    prefix=os.getenv("ROCKETMQ_TOPIC_PREFIX", "insightforge"),
                    tls=os.getenv("ROCKETMQ_TLS", "false").lower() == "true",
                    access_key=os.getenv("ROCKETMQ_ACCESS_KEY", ""),
                    secret_key=os.getenv("ROCKETMQ_SECRET_KEY", ""),
                )
                await self.transport.start()
                self.transport.require_lease = True
                self.service = TeamService(self.transport)
                self.state = PostgresTaskStateStore(self.service)
                return self.service
            except BaseException:
                await self.close()
                raise

    async def close(self) -> None:
        """Stop message callbacks before closing the PostgreSQL pool."""
        try:
            if self.transport is not None:
                await self.transport.close()
        finally:
            self.transport = None
            self.service = None
            self.state = None
            if self.pool is not None:
                await self.pool.close()
                self.pool = None


team_runtime = TeamRuntime()
