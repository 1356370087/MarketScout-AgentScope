"""Lazy asyncpg pool shared by document APIs, retrieval and workers."""

from __future__ import annotations

import asyncio
from typing import Any

import asyncpg

from .settings import get_document_settings

_pool: asyncpg.Pool | None = None
_pool_dsn: str | None = None
_pool_lock = asyncio.Lock()
_schema_checked = False
_schema_startup_error: str | None = None
_schema_signature: tuple[bool, str] | None = None
_REQUIRED_DOCUMENT_TABLES = (
    "research_documents",
    "research_document_chunks",
    "research_document_jobs",
    "research_document_worker_heartbeats",
    "research_run_sources",
    "knowledge_bases",
    "knowledge_collections",
    "knowledge_document_links",
    "research_document_versions",
    "research_document_generations",
    "research_document_units",
    "research_document_segments",
    "knowledge_entities",
    "knowledge_entity_aliases",
    "research_generation_entity_links",
    "research_document_operations",
    "knowledge_queries",
    "knowledge_usage_daily",
    "knowledge_model_attempts",
)


class DocumentSchemaError(RuntimeError):
    """Raised when the runtime document DSN has not been fully migrated."""


def _asyncpg_dsn(value: str) -> str:
    return value.replace("postgresql+asyncpg://", "postgresql://", 1)


async def get_document_pool() -> asyncpg.Pool:
    """Return a process-wide pool for the configured document database."""
    global _pool, _pool_dsn
    settings = get_document_settings()
    if not settings.configured:
        raise RuntimeError("document_research_not_configured")
    dsn = _asyncpg_dsn(settings.database_url)
    if _pool is not None and _pool_dsn == dsn:
        return _pool
    async with _pool_lock:
        if _pool is not None and _pool_dsn != dsn:
            await _pool.close()
            _pool = None
        if _pool is None:
            _pool = await asyncpg.create_pool(
                dsn, min_size=1, max_size=10, command_timeout=60
            )
            _pool_dsn = dsn
    return _pool


async def close_document_pool() -> None:
    """Close the process-wide pool during application shutdown."""
    global _pool, _pool_dsn, _schema_checked, _schema_startup_error, _schema_signature
    if _pool is not None:
        await _pool.close()
    _pool = None
    _pool_dsn = None
    _schema_checked = False
    _schema_startup_error = None
    _schema_signature = None


async def _missing_document_tables(connection: Any) -> list[str]:
    rows = await connection.fetch(
        """SELECT required.table_name,
                  to_regclass('public.' || required.table_name) IS NOT NULL AS present
           FROM unnest($1::text[]) AS required(table_name)""",
        list(_REQUIRED_DOCUMENT_TABLES),
    )
    present = {str(row["table_name"]): bool(row["present"]) for row in rows}
    return [name for name in _REQUIRED_DOCUMENT_TABLES if not present.get(name, False)]


async def assert_document_schema_ready() -> None:
    """Fail startup when the document runtime DSN lacks required tables."""
    settings = get_document_settings()
    if not settings.enabled:
        return
    if not settings.database_url:
        raise DocumentSchemaError("document_database_url_missing")
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        missing = await _missing_document_tables(connection)
    if missing:
        raise DocumentSchemaError("document_schema_missing:" + ",".join(missing))


async def initialize_document_schema(startup_error: str | None = None) -> str | None:
    """Probe document schema once and retain a document-only degraded state."""
    global _schema_checked, _schema_startup_error, _schema_signature
    settings = get_document_settings()
    _schema_signature = (settings.enabled, settings.database_url)
    if startup_error:
        _schema_startup_error = startup_error
    else:
        try:
            await assert_document_schema_ready()
        except Exception as exc:  # noqa: BLE001 - startup must preserve Web mode
            _schema_startup_error = str(exc) or type(exc).__name__
        else:
            _schema_startup_error = None
    _schema_checked = True
    return _schema_startup_error


def document_schema_available() -> bool:
    """Return whether document routes may use the configured runtime DSN."""
    settings = get_document_settings()
    if _schema_signature != (settings.enabled, settings.database_url):
        return settings.configured
    return settings.configured and (
        not _schema_checked or _schema_startup_error is None
    )


async def document_health() -> dict[str, Any]:
    """Return a bounded readiness snapshot without exposing connection details."""
    settings = get_document_settings()
    if not settings.enabled:
        return {"enabled": False, "configured": False, "database": "disabled"}
    if not settings.database_url:
        return {"enabled": True, "configured": False, "database": "missing"}
    if (
        _schema_checked
        and _schema_signature == (settings.enabled, settings.database_url)
        and _schema_startup_error
    ):
        return {
            "enabled": True,
            "configured": True,
            "database": "schema_missing"
            if _schema_startup_error.startswith("document_schema_missing:")
            else "unavailable",
            "error_code": _schema_startup_error.split(":", 1)[0],
            "worker": "unknown",
        }
    try:
        pool = await get_document_pool()
        heartbeat_timeout = max(
            1.0, get_document_settings().worker_heartbeat_seconds * 3
        )
        async with pool.acquire() as connection:
            await connection.fetchval("SELECT 1")
            missing = await _missing_document_tables(connection)
            if missing:
                return {
                    "enabled": True,
                    "configured": True,
                    "database": "schema_missing",
                    "missing_tables": missing,
                    "worker": "unknown",
                }
            worker = await connection.fetchrow(
                """SELECT worker_id, heartbeat_at
                   FROM research_document_worker_heartbeats
                   WHERE heartbeat_at >= now()-make_interval(secs=>$1)
                   ORDER BY heartbeat_at DESC LIMIT 1""",
                heartbeat_timeout,
            )
        return {
            "enabled": True,
            "configured": True,
            "database": "ready",
            "worker": "ready" if worker else "waiting",
        }
    except Exception:
        return {
            "enabled": True,
            "configured": True,
            "database": "unavailable",
            "worker": "unknown",
        }


async def document_operational_snapshot() -> dict[str, Any]:
    """Read low-cardinality queue, worker and index metrics for scraping."""
    settings = get_document_settings()
    if not settings.configured:
        return {}
    heartbeat_timeout = max(1.0, settings.worker_heartbeat_seconds * 3)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        job_rows = await connection.fetch(
            "SELECT status,count(*)::bigint AS count FROM research_document_jobs GROUP BY status"
        )
        workers = await connection.fetchval(
            """SELECT count(*) FROM research_document_worker_heartbeats
               WHERE heartbeat_at >= now()-make_interval(secs=>$1)""",
            heartbeat_timeout,
        )
        index_totals = await connection.fetchrow(
            """SELECT coalesce(sum(ocr_pages),0)::bigint AS ocr_pages,
                      coalesce(sum(chunk_count),0)::bigint AS chunks
               FROM research_documents WHERE status='ready' AND deleted_at IS NULL"""
        )
        failure_rows = await connection.fetch(
            """SELECT failure_code,count(*)::bigint AS count
               FROM research_documents
               WHERE status='failed' AND deleted_at IS NULL AND failure_code IS NOT NULL
               GROUP BY failure_code ORDER BY count(*) DESC,failure_code LIMIT 32"""
        )
    return {
        "jobs": {row["status"]: int(row["count"]) for row in job_rows},
        "workers": int(workers or 0),
        "ocr_pages": int(index_totals["ocr_pages"] or 0),
        "chunks": int(index_totals["chunks"] or 0),
        "failures": {
            row["failure_code"]: int(row["count"]) for row in failure_rows
        },
    }
