"""隔离的原生服务存储接入（M2/AS-T011）。

提供三种形态：

- **PostgreSQL 完整模式**：业务库内独立 schema（默认 ``agentscope_runtime``），
  框架 11 张表与业务表（含同名 ``knowledge_bases``）互不影响（M1/T005 已实证）。
  生产要求 ``AS_STORAGE_AUTO_CREATE=false``，schema 由显式迁移命令创建。
- **开发模式**：同库独立 schema，允许启动时 ``create_all``/自动迁移。
- **演示模式**：无 PostgreSQL 时退回本地 SQLite 文件（``.runs/``）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from open_deep_research.agentscope_runtime.settings import ASRuntimeSettings

RUNTIME_REPO_ROOT = Path(__file__).resolve().parents[3]


def runtime_data_dir() -> Path:
    """Use the application data volume, including non-root container deployments."""
    import os

    return Path(os.environ.get("RUNS_DIR") or RUNTIME_REPO_ROOT / ".runs").resolve()


def _sqlite_demo_path() -> Path:
    db = runtime_data_dir() / "agentscope-runtime.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    return db


def build_engine_kwargs(settings: ASRuntimeSettings) -> dict[str, Any]:
    """PostgreSQL 连接参数：把框架表固定在独立 schema。"""
    if settings.is_demo:
        return {}
    return {
        "connect_args": {
            "server_settings": {"search_path": f"{settings.database_schema},public"},
        },
    }


def build_storage(settings: ASRuntimeSettings | None = None):
    """构造框架存储实例（未连接；经 ``async with`` 打开）。"""
    from agentscope.app.storage._sql import AsyncSQLAlchemyStorage

    settings = settings or ASRuntimeSettings.from_env()
    if settings.is_demo:
        db = _sqlite_demo_path()
        return AsyncSQLAlchemyStorage(
            f"sqlite+aiosqlite:///{db.as_posix()}",
            create_tables=settings.storage_auto_create or True,
            auto_migrate=False,
        )
    return AsyncSQLAlchemyStorage(
        settings.database_url,
        create_tables=settings.storage_auto_create,
        auto_migrate=False,
        engine_kwargs=build_engine_kwargs(settings),
    )


async def ensure_runtime_schema(engine: AsyncEngine, schema: str) -> None:
    """CREATE SCHEMA IF NOT EXISTS（create_all 不会自建 schema）。"""
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))


async def run_storage_migrations(settings: ASRuntimeSettings | None = None) -> None:
    """显式迁移入口（生产部署步骤；等价 alembic upgrade head，指向框架内置脚本）。

    只在部署/运维时调用一次；多副本生产禁止依赖启动期自动迁移
    （两个副本同时迁移不安全，见 AsyncSQLAlchemyStorage.auto_migrate 文档）。
    """
    from agentscope.app.storage._sql import AsyncSQLAlchemyStorage
    from sqlalchemy.ext.asyncio import create_async_engine

    settings = settings or ASRuntimeSettings.from_env()
    if settings.is_demo:
        # 演示模式无独立迁移概念：直接建表。
        async with build_storage(settings):
            return
    engine = create_async_engine(settings.database_url, **build_engine_kwargs(settings))
    try:
        await ensure_runtime_schema(engine, settings.database_schema)
        # SDK 的 auto_migrate 会另建连接且丢弃 engine_kwargs，误读 public
        # 中 IAM 的 alembic_version。复用隔离连接执行同一套框架迁移。
        import inspect

        from alembic.config import Config
        from alembic.runtime.environment import EnvironmentContext
        from alembic.script import ScriptDirectory

        migration_dir = Path(inspect.getfile(AsyncSQLAlchemyStorage)).parent / "_alembic"
        migration_config = Config()
        migration_config.set_main_option("script_location", str(migration_dir))
        scripts = ScriptDirectory.from_config(migration_config)

        def migrate(connection):
            with EnvironmentContext(
                migration_config, scripts,
                fn=lambda revision, context: scripts._upgrade_revs("head", revision),
            ) as context:
                context.configure(
                    connection=connection,
                    version_table_schema=settings.database_schema,
                )
                with context.begin_transaction():
                    context.run_migrations()

        async with engine.begin() as connection:
            await connection.run_sync(migrate)
    finally:
        await engine.dispose()
    storage = AsyncSQLAlchemyStorage(
        settings.database_url,
        create_tables=False,
        auto_migrate=False,
        engine_kwargs=build_engine_kwargs(settings),
    )
    async with storage:
        from open_deep_research.agentscope_runtime.durable import DurableCommandBridge
        from open_deep_research.agentscope_runtime.pgbus import PostgreSQLMessageBus
        async with PostgreSQLMessageBus(
            settings.database_url, table_prefix=settings.bus_table_prefix,
            engine_kwargs=build_engine_kwargs(settings),
        ), DurableCommandBridge(
            settings.database_url, table_prefix=settings.bus_table_prefix,
            engine_kwargs=build_engine_kwargs(settings),
        ):
            pass
        from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
        recovery = RecoveryStore(settings.database_url, engine_kwargs=build_engine_kwargs(settings))
        try:
            await recovery.create_tables()
        finally:
            await recovery.aclose()
