"""T054 HTTP quotas and durable ingestion recovery against an isolated database."""

import asyncio
import os
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from open_deep_research.documents import repository, router, worker
from open_deep_research.documents.database import (
    close_document_pool,
    get_document_pool,
    initialize_document_schema,
)
from security.rbac.principal import Principal

DSN = os.environ.get("IAM_TEST_DATABASE_URL", "")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not DSN, reason="requires isolated PostgreSQL"),
]


async def test_upload_quota_retry_expired_job_and_native_worker(monkeypatch, tmp_path):
    owner = str(uuid4())
    monkeypatch.setenv("DOCUMENT_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("DOCUMENT_DATABASE_URL", DSN)
    monkeypatch.setenv("DOCUMENT_STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("DOCUMENT_MAX_COUNT_PER_USER", "1")
    monkeypatch.setenv("DOCLING_SERVE_URL", "")
    assert await initialize_document_schema() is None
    pool = await get_document_pool()
    principal = Principal(
        user_id=owner,
        email="ingestion@test.invalid",
        status="active",
        session_id=None,
        roles=frozenset(),
        permissions=frozenset(),
        authz_version=0,
    )
    app = FastAPI()
    app.include_router(router.router)
    for route in router.router.routes:
        for dependency in route.dependant.dependencies:
            if dependency.name == "user":
                app.dependency_overrides[dependency.call] = lambda: principal

    async def embed(texts, *_args, **_kwargs):
        return [[0.1] * 1536 for _ in texts]

    async def suggestions(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(worker, "embed_texts", embed)
    monkeypatch.setattr(worker, "build_suggestions", suggestions)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            file = {
                "file": ("research.txt", b"Evidence about market demand.", "text/plain")
            }
            first = await client.post("/documents", files=file)
            assert first.status_code == 202, first.text
            doc_id = first.json()["document"]["id"]
            duplicate = await client.post("/documents", files=file)
            assert duplicate.status_code == 202 and duplicate.json()["deduplicated"]
            over = await client.post(
                "/documents",
                files={"file": ("other.txt", b"Other evidence", "text/plain")},
            )
            assert over.status_code == 413, over.text

        abandoned = await repository.claim_job("abandoned", 1)
        assert str(abandoned["document_id"]) == doc_id
        assert await repository.record_job_upstream_task(
            str(abandoned["id"]), "abandoned", "upstream-receipt"
        )
        await asyncio.sleep(1.1)  # Real SQL lease expiration, no fence/time rewrite.
        takeover = await repository.claim_job("replacement", 30)
        assert takeover["id"] == abandoned["id"]
        assert takeover["upstream_task_id"] == "upstream-receipt"
        assert not await repository.complete_job(str(takeover["id"]), "abandoned")
        assert await repository.fail_job(
            takeover, "acceptance_transient", 1, "replacement"
        )
        retried = await asyncio.gather(
            repository.retry_document(owner, doc_id),
            repository.retry_document(owner, doc_id),
        )
        assert sum(item is not None for item in retried) == 1
        assert await worker._process("native-worker")
        async with pool.acquire() as connection:
            generation = await connection.fetchrow(
                "SELECT status FROM research_document_generations WHERE document_id=$1::uuid ORDER BY created_at DESC LIMIT 1",
                doc_id,
            )
            assert generation["status"] == "pending_review"
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM research_document_segments s JOIN research_document_generations g ON g.id=s.generation_id WHERE g.document_id=$1::uuid",
                    doc_id,
                )
                > 0
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM research_document_jobs WHERE document_id=$1::uuid AND status IN ('queued','running')",
                    doc_id,
                )
                == 0
            )
    finally:
        async with pool.acquire() as connection:
            await connection.execute(
                "DELETE FROM research_documents WHERE owner_id=$1::uuid", owner
            )
            await connection.execute(
                "DELETE FROM knowledge_bases WHERE owner_id=$1::uuid", owner
            )
        await close_document_pool()
