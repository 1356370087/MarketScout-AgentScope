"""Native migrations must not read or modify the co-located IAM version."""

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from open_deep_research.agentscope_runtime.settings import ASRuntimeSettings
from open_deep_research.agentscope_runtime.storage import run_storage_migrations


@pytest.mark.asyncio
async def test_migrations_isolate_iam_revision_and_are_repeatable(pg_url):
    schema = "migration_" + uuid4().hex
    engine = create_async_engine(pg_url)
    settings = ASRuntimeSettings(pg_url, schema, False, "as_bus_")
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE public.alembic_version (version_num varchar(64) PRIMARY KEY)"))
            await conn.execute(text("INSERT INTO public.alembic_version VALUES ('0016_research_teams')"))
        await run_storage_migrations(settings)
        await run_storage_migrations(settings)
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT version_num FROM public.alembic_version")) == "0016_research_teams"
            assert await conn.scalar(text(f'SELECT count(*) FROM "{schema}".alembic_version')) == 1
            assert await conn.scalar(text("SELECT count(*) FROM information_schema.tables WHERE table_schema = :schema"), {"schema": schema}) > 10
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await conn.execute(text("DROP TABLE IF EXISTS public.alembic_version"))
        await engine.dispose()
