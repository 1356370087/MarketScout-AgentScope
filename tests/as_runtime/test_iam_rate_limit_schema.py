"""Login counters use their business key after the UUID primary-key migration."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from security.rbac.models import RateLimit, utcnow
from security.rbac.services import rate_limit


@pytest.mark.asyncio
async def test_rate_limit_business_key_on_uuid_primary_key(pg_url):
    schema = "team_iam_" + uuid4().hex
    engine = create_async_engine(pg_url, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.run_sync(RateLimit.__table__.create)
        async with AsyncSession(engine) as session:
            assert await rate_limit.current_count(session, bucket="login", identity="e2e") == 0
            assert await rate_limit.check_and_consume(session, bucket="login", identity="e2e", limit=3, window_seconds=600) == 1
            await session.commit()
        async with AsyncSession(engine) as session:
            assert await rate_limit.current_count(session, bucket="login", identity="e2e", window_seconds=600) == 1
            assert await rate_limit.current_count(session, bucket="login", identity="another") == 0
            await session.execute(update(RateLimit).values(window_started_at=utcnow() - timedelta(seconds=700)))
            await session.commit()
        async with AsyncSession(engine) as session:
            assert await rate_limit.current_count(session, bucket="login", identity="e2e", window_seconds=600) == 0
            assert await rate_limit.current_count(session, bucket="login", identity="e2e") == 1
            await rate_limit.reset(session, bucket="login", identity="e2e")
            await session.commit()
        async with AsyncSession(engine) as session:
            assert await rate_limit.current_count(session, bucket="login", identity="e2e") == 0
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()
