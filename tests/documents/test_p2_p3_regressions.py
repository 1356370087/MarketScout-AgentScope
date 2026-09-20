"""Regression tests for the P2/P3 local-document and source-boundary fixes."""

from __future__ import annotations

import ast
import json
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from open_deep_research.documents import database, embeddings, repository, versioning
from open_deep_research.documents.contracts import URLSourceRef
from open_deep_research.documents.router import upload_document
from open_deep_research.documents.settings import DocumentSettings
from open_deep_research.documents.storage import DocumentUploadError
from open_deep_research.quality.gate import deterministic_tool_checks
from open_deep_research.report.models import SourceRef
from open_deep_research.report.orchestrator import (
    _has_verifiable_body_citation,
    _is_internal_source,
)
from open_deep_research.tools.governance import AgentRole
from open_deep_research.tools.registry import prepare_existing_toolset
from open_deep_research.tools.research_complete import research_complete
from open_deep_research.agentscope_runtime.research_agents import _control_tool, _Thought
from open_deep_research.tools.base import ToolContext, ToolResult
from open_deep_research.agentscope_runtime import web_tools as pipeline
from open_deep_research.web.models import SearchRequest, WebResearchResult, GapAnalysis, BudgetSnapshot
from open_deep_research.web.pipeline import rank_candidates


async def _thought(*args):
    return ToolResult(output="thought")


think_tool = _control_tool("think_tool", _Thought, _thought)


async def _discover_native(monkeypatch, request, config, fake_search):
    batches = []

    class Client:
        async def search(self, query, **kwargs):
            results = await fake_search([query], **kwargs)
            return results[0] if results else {"query": query, "results": []}

    class Pipeline:
        def __init__(self, *, search, **kwargs):
            self.search = search

        async def run(self, request, **kwargs):
            batches.append(await self.search(request))
            return WebResearchResult(request=request, gap_analysis=GapAnalysis(
                decision="complete", reason="fixture", budget=BudgetSnapshot()))

    monkeypatch.setattr(pipeline, "WebResearchPipeline", Pipeline)
    tool = pipeline.web_research_tool(lambda: config, None, pipeline.WebFetchLedger(),
                                      tavily_client_factory=lambda _: Client())
    await tool.call(tool.input_schema(objective=request.objective, queries=request.queries),
                    ToolContext(config=config, role="researcher", tool_call_id="source-discovery"))
    return batches[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_code", "expected_status"),
    [("document_file_too_large", 413), ("document_file_empty", 422)],
)
async def test_upload_errors_use_semantic_http_statuses(
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
    expected_status: int,
) -> None:
    """Size and validation failures must be distinguishable by HTTP status."""
    import open_deep_research.documents.router as document_router

    monkeypatch.setattr(document_router, "_ensure_enabled", lambda: None)

    async def fail_stage(*_args: Any, **_kwargs: Any) -> None:
        raise DocumentUploadError(error_code)

    monkeypatch.setattr(document_router, "stage_upload", fail_stage)

    with pytest.raises(HTTPException) as raised:
        await upload_document(
            SimpleNamespace(filename="sample.txt"),
            SimpleNamespace(user_id="00000000-0000-0000-0000-000000000001"),
        )

    assert raised.value.status_code == expected_status
    assert raised.value.detail == error_code


@pytest.mark.asyncio
async def test_specific_url_matching_uses_canonical_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trailing slash or provider host casing must not reject an exact URL."""
    captured: list[list[str]] = []

    async def fake_search(queries: list[str], **_kwargs: Any) -> list[dict[str, Any]]:
        captured.append(list(queries))
        return [
            {
                "query": queries[0],
                "results": [
                    {
                        "url": "https://EXAMPLE.com/report",
                        "title": "Report",
                        "content": "supported result",
                    }
                ],
            }
        ]

    config = {
        "configurable": {"search_api": "tavily"},
        "metadata": {
            "source_selection": {
                "mode": "specific",
                "sources": [
                    {"type": "url", "url": "https://example.com/report/"},
                    {"type": "domain", "domain": "search.example.com"},
                ],
            }
        },
    }

    batch = await _discover_native(monkeypatch,
        SearchRequest(objective="report", queries=["report"], candidate_limit=10),
        config, fake_search,
    )

    assert captured
    assert any(candidate.provider == "tavily" for candidate in batch.candidates)


@pytest.mark.asyncio
async def test_specific_exact_url_is_not_rejected_by_authority_threshold() -> None:
    """An explicitly selected URL must remain fetchable on ordinary domains."""
    candidate = pipeline._candidate(  # noqa: SLF001
        "specific_url",
        "https://example.com/report/",
        "https://example.com/report/",
        "Explicit URL selected by the user",
        1,
        "specific-url",
    )
    assert candidate is not None

    ranked = await rank_candidates(
        "report",
        [candidate],
        top_k=1,
        min_authority=0.65,
    )

    assert ranked[0].selected is True


def test_document_quality_counts_distinct_documents_not_chunks() -> None:
    """Multiple chunks from one local document count as one source."""
    payload = {
        "documents": [
            {
                "document_id": "doc-1",
                "source_type": "local_document",
                "source_uri": "/documents/doc-1",
            }
        ],
        "evidence": [
            {
                "document_id": "doc-1",
                "chunk_id": "chunk-1",
                "source_type": "local_document",
                "source_url": "/documents/doc-1?chunk=chunk-1",
                "security_status": "accepted",
            },
            {
                "document_id": "doc-1",
                "chunk_id": "chunk-2",
                "source_type": "local_document",
                "source_url": "/documents/doc-1?chunk=chunk-2",
                "security_status": "accepted",
            },
        ],
    }

    checks = deterministic_tool_checks(
        [{"name": "search_documents", "content": json.dumps(payload), "error": False}],
        min_sources=1,
    )

    assert checks["passed"] is True
    assert checks["source_count"] == 1
    assert checks["structured_evidence_count"] == 2


@pytest.mark.asyncio
async def test_specific_domain_queries_keep_the_full_cartesian_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every selected-domain/query pair must reach the search provider."""
    captured: list[list[str]] = []

    async def fake_search(queries: list[str], **_kwargs: Any) -> list[dict[str, Any]]:
        captured.append(list(queries))
        return []

    domains = [f"source-{index}.example.com" for index in range(4)]
    config = {
        "configurable": {"search_api": "tavily"},
        "metadata": {
            "source_selection": {
                "mode": "specific",
                "sources": [{"type": "domain", "domain": domain} for domain in domains],
            }
        },
    }

    await _discover_native(monkeypatch,
        SearchRequest(
            objective="report",
            queries=["market", "risk", "outlook"],
            candidate_limit=10,
        ),
        config, fake_search,
    )

    assert [query for batch in captured for query in batch] == [
        f"site:{domain} {query}"
        for domain in domains
        for query in ("market", "risk", "outlook")
    ]


@pytest.mark.asyncio
async def test_specific_domain_queries_are_bounded_with_an_overflow_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Large allowlists must not turn one tool call into an unbounded fan-out."""
    captured: list[list[str]] = []

    async def fake_search(queries: list[str], **_kwargs: Any) -> list[dict[str, Any]]:
        captured.append(list(queries))
        return []

    domains = [f"source-{index}.example.com" for index in range(100)]
    config = {
        "configurable": {"search_api": "tavily"},
        "metadata": {
            "source_selection": {
                "mode": "specific",
                "sources": [{"type": "domain", "domain": domain} for domain in domains],
            }
        },
    }

    batch = await _discover_native(monkeypatch,
        SearchRequest(
            objective="report",
            queries=["market", "risk", "outlook"],
            candidate_limit=10,
        ),
        config, fake_search,
    )

    assert captured
    assert sum(map(len, captured)) == pipeline.MAX_SPECIFIC_DOMAIN_QUERIES
    assert sum(map(len, captured)) < len(domains) * 3
    assert any("specific_domain_query_limit_exceeded" in error for error in batch.errors)


class _RepositoryConnection:
    def __init__(self, *, row: dict[str, Any] | None = None) -> None:
        self.row = row
        self.sql: list[str] = []
        self.arguments: list[tuple[Any, ...]] = []

    async def fetchrow(self, sql: str, *arguments: Any) -> dict[str, Any] | None:
        self.sql.append(sql)
        self.arguments.append(arguments)
        if "FOR UPDATE" in sql:
            return self.row
        if "UPDATE research_documents" in sql and "RETURNING" in sql:
            return self.row
        return None

    async def fetchval(self, sql: str, *_arguments: Any) -> bool:
        self.sql.append(sql)
        return False

    async def execute(self, sql: str, *_arguments: Any) -> str:
        self.sql.append(sql)
        return "INSERT 0 1"

    @asynccontextmanager
    async def transaction(self):
        yield


class _RepositoryPool:
    def __init__(self, connection: _RepositoryConnection) -> None:
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@pytest.mark.asyncio
async def test_retry_rejects_an_existing_inflight_ingest_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry SQL must exclude queued/running jobs for the same document."""
    connection = _RepositoryConnection()

    async def fake_pool() -> _RepositoryPool:
        return _RepositoryPool(connection)

    monkeypatch.setattr(repository, "get_document_pool", fake_pool)
    await repository.retry_document(
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
    )

    update_sql = connection.sql[0]
    assert "NOT EXISTS" in update_sql
    assert "status IN ('queued','running')" in update_sql


def _document_row() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "id": "00000000-0000-0000-0000-000000000002",
        "filename": "report.txt",
        "media_type": "text/plain",
        "size_bytes": 12,
        "sha256": "a" * 64,
        "storage_key": "owner/report.txt",
        "status": "ready",
        "failure_code": None,
        "page_count": 1,
        "chunk_count": 1,
        "ocr_pages": 0,
        "created_at": now,
        "updated_at": now,
        "deleted_at": None,
    }


@pytest.mark.asyncio
async def test_reindex_creates_a_draft_generation_and_durable_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pure index rebuild must queue a fresh draft generation, not publish."""
    version_id = "00000000-0000-0000-0000-000000000003"
    generation_id = "00000000-0000-0000-0000-000000000004"
    fetchval_results = [False, version_id, generation_id]
    sql: list[str] = []

    class Connection:
        async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
            sql.append(query)
            if "FOR UPDATE" in query:
                return {**_document_row(), "current_generation_id": None}
            return None

        async def fetchval(self, query: str, *args: Any) -> Any:
            sql.append(query)
            return fetchval_results.pop(0)

        async def execute(self, query: str, *args: Any) -> str:
            sql.append(query)
            return "INSERT 0 1"

        @asynccontextmanager
        async def transaction(self):
            yield

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    async def fake_pool() -> Pool:
        return Pool()

    monkeypatch.setattr(versioning, "get_document_pool", fake_pool)
    result = await versioning.queue_reindex_generation(
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
    )

    assert result == generation_id
    assert any("INSERT INTO research_document_generations" in query for query in sql)
    assert any("'reindex'" in query for query in sql)


class _FakeEmbeddingAPI:
    async def create(self, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            data=[SimpleNamespace(index=0, embedding=[0.1, 0.2])]
        )


class _PooledEmbeddingClient:
    constructed = 0
    closed = 0

    def __init__(self, **_kwargs: Any) -> None:
        type(self).constructed += 1
        self.embeddings = _FakeEmbeddingAPI()

    async def close(self) -> None:
        type(self).closed += 1


@pytest.mark.asyncio
async def test_embedding_client_is_reused_between_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """High-frequency embedding calls should share a process-level client."""
    _PooledEmbeddingClient.constructed = 0
    _PooledEmbeddingClient.closed = 0
    monkeypatch.setenv("LITELLM_BASE_URL", "http://embedding-pool-regression.test/v1")
    monkeypatch.setenv("LITELLM_SERVICE_KEY", "sk-service-regression")
    monkeypatch.setattr(embeddings, "AsyncOpenAI", _PooledEmbeddingClient)
    monkeypatch.setattr(embeddings, "_embedding_clients", {}, raising=False)
    monkeypatch.setattr(embeddings, "_embedding_clients_lock", threading.RLock(), raising=False)

    settings = DocumentSettings(embedding_dimensions=2)
    await embeddings.embed_texts(["one"], settings)
    await embeddings.embed_texts(["two"], settings)

    assert _PooledEmbeddingClient.constructed == 1
    assert _PooledEmbeddingClient.closed == 0
    assert list(embeddings._embedding_clients) == [  # noqa: SLF001
        ("http://embedding-pool-regression.test/v1", "sk-service-regression")
    ]
    if hasattr(embeddings, "close_embedding_clients"):
        await embeddings.close_embedding_clients()


@pytest.mark.asyncio
async def test_non_ingest_embedding_operations_require_a_run_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LITELLM_BASE_URL", "http://embedding-operation.test/v1")
    monkeypatch.setenv("LITELLM_SERVICE_KEY", "sk-service-regression")
    monkeypatch.setattr(
        embeddings,
        "current_run_key",
        lambda: (_ for _ in ()).throw(RuntimeError("litellm_run_key_unavailable")),
    )

    with pytest.raises(
        embeddings.EmbeddingError, match="document_query_run_key_unavailable"
    ):
        await embeddings.embed_texts(
            ["query"], DocumentSettings(embedding_dimensions=2), operation="retrieval"
        )


@pytest.mark.asyncio
async def test_document_mode_without_document_tool_permission_fails_fast() -> None:
    """Permission filtering must not leave a document run to spin idle."""
    config = {
        "metadata": {
            "source_selection": {
                "mode": "documents",
                "sources": [{"type": "document", "id": "doc-1"}],
            }
        }
    }

    with pytest.raises(ValueError, match="document_mode_requires_search_documents"):
        await prepare_existing_toolset(
            [research_complete, think_tool], AgentRole.RESEARCHER, config
        )


@pytest.mark.asyncio
async def test_document_mode_supervisor_does_not_require_researcher_tool() -> None:
    """Supervisor control tools remain valid without local search itself."""
    config = {
        "metadata": {
            "source_selection": {
                "mode": "documents",
                "sources": [{"type": "document", "id": "doc-1"}],
            }
        }
    }

    assembly = await prepare_existing_toolset(
        [research_complete, think_tool], AgentRole.SUPERVISOR, config
    )

    assert {tool.name for tool in assembly.tools} == {"ResearchComplete", "think_tool"}


@pytest.mark.asyncio
async def test_document_schema_probe_accepts_migration_mismatch_as_degraded_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An incomplete document migration must degrade only document features."""
    monkeypatch.setenv("DOCUMENT_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("DOCUMENT_DATABASE_URL", "postgresql://documents.test/db")
    monkeypatch.setattr(database, "_schema_checked", False)
    monkeypatch.setattr(database, "_schema_startup_error", None)

    error = await database.initialize_document_schema(
        "schema_revision_mismatch:got=0002_sandbox_permissions:expected=0003_local_documents"
    )

    assert error is not None
    assert database.document_schema_available() is False


def test_report_internal_source_detection_uses_explicit_type() -> None:
    """Metrics must not infer provenance from an opaque URI string."""
    source = SourceRef(
        title="Internal report",
        url="/documents/doc-1?chunk=chunk-1",
        source_type="local_document",
        document_id="doc-1",
        chunk_id="chunk-1",
    )
    assert _is_internal_source(source)
    assert _is_internal_source(
        SourceRef(title="Legacy internal", url="/documents/doc-legacy", source_type="local_document")
    )
    assert not _is_internal_source(SourceRef(title="Web", url="/documents/not-internal"))


def test_local_document_link_counts_as_a_verifiable_report_citation() -> None:
    assert _has_verifiable_body_citation(
        "内部结论见 [原始片段](/documents/doc-1?chunk=chunk-1)",
        allowed_urls={"/documents/doc-1?chunk=chunk-1"},
        source_count=1,
    )
    assert _has_verifiable_body_citation(
        "内部结论见 [原始片段](/documents/doc-1/chunks/chunk-1)",
        allowed_urls={"/documents/doc-1?chunk=chunk-1"},
        source_count=1,
    )


@pytest.mark.asyncio
async def test_specific_hostname_is_checked_against_resolved_private_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Specific source admission still relies on the DNS-aware network gate."""
    import socket

    from open_deep_research.security import network

    monkeypatch.setattr(
        network.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ],
    )
    URLSourceRef(type="url", url="https://docs.example.com/report")
    with pytest.raises(ValueError, match="Hostname resolves to a private"):
        await network.validate_public_http_url("https://docs.example.com/report")


def test_document_delete_route_does_not_shadow_builtin() -> None:
    """Route handlers should not use the built-in name ``delete``."""
    path = Path(__file__).parents[2] / "src" / "open_deep_research" / "documents" / "router.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "delete"
        for node in ast.walk(tree)
    )


def test_source_url_normalization_handles_public_ipv6_and_default_ports() -> None:
    source = URLSourceRef(type="url", url="HTTPS://[2001:4860:4860::8888]:443/report/")
    assert source.url == "https://[2001:4860:4860::8888]/report"


def test_source_url_normalization_rejects_dotted_private_ip_literals() -> None:
    with pytest.raises(ValueError, match="source_url_private_host_not_allowed"):
        URLSourceRef(type="url", url="http://127.0.0.1./admin")
