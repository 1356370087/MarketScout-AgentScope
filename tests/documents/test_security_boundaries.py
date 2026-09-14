"""Regression tests for document upload, credentials, retrieval and schema gates."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.requests import Request
from starlette.responses import Response

from open_deep_research import server
from open_deep_research.documents import database, embeddings, retrieval
from open_deep_research.documents.settings import DocumentSettings


@pytest.mark.asyncio
async def test_document_upload_without_content_length_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chunked multipart uploads must be rejected before route parsing starts."""
    monkeypatch.setenv("DOCUMENT_MAX_FILE_BYTES", "1024")
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/documents",
            "raw_path": b"/documents",
            "query_string": b"",
            "headers": [(b"content-type", b"multipart/form-data; boundary=test")],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
    )
    route_called = False

    async def call_next(_request: Request) -> Response:
        nonlocal route_called
        route_called = True
        return Response(status_code=204)

    response = await server.request_body_limit_middleware(request, call_next)

    assert response.status_code == 411
    assert json.loads(response.body) == {"detail": "content_length_required"}
    assert route_called is False


class _FakeEmbeddingsAPI:
    async def create(self, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            data=[SimpleNamespace(index=0, embedding=[0.1, 0.2])]
        )


class _FakeEmbeddingClient:
    def __init__(self, **_kwargs: Any) -> None:
        self.embeddings = _FakeEmbeddingsAPI()

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_query_embedding_never_falls_back_to_master_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A query outside a Run Key context must fail before gateway I/O."""
    monkeypatch.setenv("LITELLM_BASE_URL", "http://gateway.test/v1")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-master-must-not-be-used")
    monkeypatch.setattr(
        embeddings,
        "current_run_key",
        lambda: (_ for _ in ()).throw(RuntimeError("litellm_run_key_unavailable")),
    )
    monkeypatch.setattr(embeddings, "AsyncOpenAI", _FakeEmbeddingClient)

    with pytest.raises(
        embeddings.EmbeddingError, match="document_query_run_key_unavailable"
    ):
        await embeddings.embed_texts(
            ["中文查询"],
            DocumentSettings(embedding_dimensions=2),
            operation="query",
        )


class _RetrievalConnection:
    def __init__(self) -> None:
        self.sql = ""
        self.arguments: tuple[Any, ...] = ()

    async def fetch(self, sql: str, *arguments: Any) -> list[dict[str, Any]]:
        self.sql = sql
        self.arguments = arguments
        return []


class _RetrievalPool:
    def __init__(self, connection: _RetrievalConnection) -> None:
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@pytest.mark.asyncio
async def test_chinese_retrieval_uses_word_similarity_candidate_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Long Chinese queries must not depend on the default `%` threshold."""
    connection = _RetrievalConnection()

    async def fake_embed(*_args: Any, **_kwargs: Any) -> list[list[float]]:
        return [[0.1, 0.2]]

    async def fake_pool() -> _RetrievalPool:
        return _RetrievalPool(connection)

    monkeypatch.setattr(retrieval, "embed_texts", fake_embed)
    monkeypatch.setattr(retrieval, "get_document_pool", fake_pool)

    await retrieval.search_document_chunks(
        owner_id="00000000-0000-0000-0000-000000000001",
        document_ids=["00000000-0000-0000-0000-000000000002"],
        query="中国新能源汽车市场规模增长率与竞争格局",
        api_key="sk-run",
    )

    assert "text %>> $4" in connection.sql
    assert "research_document_segments" in connection.sql
    assert "current_generation_id" in connection.sql
    assert "eligible AS MATERIALIZED" not in connection.sql
    assert "word_similarity($4,text)" in connection.sql
    assert "text % $4" not in connection.sql
    assert connection.arguments[-2] == pytest.approx(0.08)


class _SchemaConnection:
    async def fetch(self, _sql: str, *_arguments: Any) -> list[dict[str, Any]]:
        return [
            {"table_name": "research_documents", "present": True},
            {"table_name": "research_document_chunks", "present": False},
        ]


class _SchemaPool:
    @asynccontextmanager
    async def acquire(self):
        yield _SchemaConnection()


@pytest.mark.asyncio
async def test_document_startup_schema_check_skips_when_feature_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The document gate must not block the backward-compatible Web mode."""
    monkeypatch.setenv("DOCUMENT_RESEARCH_ENABLED", "false")

    async def unexpected_pool() -> _SchemaPool:
        raise AssertionError("disabled document research must not access PostgreSQL")

    monkeypatch.setattr(database, "get_document_pool", unexpected_pool)

    await database.assert_document_schema_ready()


@pytest.mark.asyncio
async def test_document_startup_schema_check_rejects_split_dsn_missing_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A document DSN without the migrated core tables must fail explicitly."""
    monkeypatch.setenv("DOCUMENT_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("DOCUMENT_DATABASE_URL", "postgresql://documents.test/db")

    async def fake_pool() -> _SchemaPool:
        return _SchemaPool()

    monkeypatch.setattr(database, "get_document_pool", fake_pool)

    with pytest.raises(
        database.DocumentSchemaError,
        match="document_schema_missing:research_document_chunks",
    ):
        await database.assert_document_schema_ready()


@pytest.mark.asyncio
async def test_document_startup_probe_degrades_documents_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken document DSN must not prevent the Web-only API from starting."""
    monkeypatch.setenv("DOCUMENT_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("DOCUMENT_DATABASE_URL", "postgresql://documents.test/db")
    monkeypatch.setattr(database, "_schema_checked", False)
    monkeypatch.setattr(database, "_schema_startup_error", None)

    async def fake_pool() -> _SchemaPool:
        return _SchemaPool()

    monkeypatch.setattr(database, "get_document_pool", fake_pool)

    error = await database.initialize_document_schema()

    assert error == (
        "document_schema_missing:research_document_chunks,"
        "research_document_jobs,research_document_worker_heartbeats,"
        "research_run_sources,knowledge_bases,knowledge_collections,"
        "knowledge_document_links,research_document_versions,"
        "research_document_generations,research_document_units,"
        "research_document_segments,knowledge_entities,knowledge_entity_aliases,"
        "research_generation_entity_links,research_document_operations,"
        "knowledge_queries"
    )
    assert database.document_schema_available() is False
