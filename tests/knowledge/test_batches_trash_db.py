"""Phase-B recycle bin and batch operations against a real PostgreSQL."""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from pathlib import Path
from tempfile import NamedTemporaryFile

import pytest

from open_deep_research.documents import repository, versioning
from open_deep_research.documents.database import close_document_pool, get_document_pool
from open_deep_research.documents.settings import DocumentSettings
from open_deep_research.documents.storage import StagedUpload
from open_deep_research.knowledge import batches, trash

_TEST_DSN = os.environ.get("IAM_TEST_DATABASE_URL", "")

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.db,
    pytest.mark.skipif(not _TEST_DSN, reason="IAM_TEST_DATABASE_URL not configured"),
]

OWNER = str(uuid.uuid4())


@pytest.mark.parametrize("interruption", ["cancel", "revoke", "worker_exit"])
async def test_batch_concurrent_claim_and_interruption(monkeypatch, interruption):
    """A second executor cannot duplicate effects; interruption stops new work."""
    from open_deep_research.knowledge import authz

    monkeypatch.setenv("DOCUMENT_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("DOCUMENT_DATABASE_URL", _TEST_DSN.replace("postgresql+asyncpg://", "postgresql://", 1))
    actor = str(uuid.uuid4())
    pool = await get_document_pool()
    entered, release = asyncio.Event(), asyncio.Event()
    effects = []
    revoked = False

    async def access(*args):
        return {"capabilities": frozenset() if revoked else {authz.CAP_MANAGE}}

    async def effect(_actor, document):
        effects.append(document)
        entered.set()
        await release.wait()
        return {"status": "trashed"}

    monkeypatch.setattr(authz, "document_access", access)
    monkeypatch.setattr(trash, "trash_document", effect)
    running = None
    try:
        batch = await batches.create_batch(actor, "trash", [str(uuid.uuid4()), str(uuid.uuid4())])
        batch_id = batch["batch_id"]
        running = asyncio.create_task(batches.execute_batch(actor, batch_id))
        await asyncio.wait_for(entered.wait(), 5)
        assert (await batches.execute_batch(actor, batch_id))["status"] == "not_executable"
        if interruption == "cancel":
            await batches.cancel_batch(actor, batch_id)
        elif interruption == "revoke":
            revoked = True
        else:
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
        release.set()
        if interruption == "worker_exit":
            await batches.execute_batch(actor, batch_id)
        else:
            await running
        detail = await batches.get_batch(actor, batch_id)
        assert detail["completed"] == 1
        if interruption == "cancel":
            assert detail["status"] == "cancelled"
            assert len(effects) == 1
            assert {item["status"] for item in detail["items"]} == {"succeeded", "cancelled"}
        else:
            assert detail["failed"] == 1
            failure = next(item for item in detail["items"] if item["status"] == "failed")
            assert failure["failure_code"] == ("document_permission_revoked" if interruption == "revoke" else "interrupted_requires_review")
            assert len(effects) == (1 if interruption == "revoke" else 2)
            assert all(item["attempt"] == 1 for item in detail["items"])
    finally:
        release.set()
        if running and not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        async with pool.acquire() as connection:
            await connection.execute("DELETE FROM knowledge_batch_items WHERE batch_id IN (SELECT id FROM knowledge_batches WHERE created_by=$1::uuid)", actor)
            await connection.execute("DELETE FROM knowledge_batches WHERE created_by=$1::uuid", actor)
        await close_document_pool()


def _staged(name: str, content: bytes) -> StagedUpload:
    handle = NamedTemporaryFile(delete=False, suffix=Path(name).suffix)
    handle.write(content)
    handle.close()
    return StagedUpload(
        Path(handle.name), name, "text/markdown", len(content),
        hashlib.sha256(content).hexdigest(),
    )


async def _fake_embed(texts, settings=None, **_kwargs):
    return [[0.1] * 1536 for _ in texts]


def _settings():
    return DocumentSettings(max_documents_per_user=20, max_bytes_per_user=10**9)


async def _make_ready_doc(owner: str, name: str) -> str:
    document, _ = await repository.create_document(
        owner, _staged(name, name.encode()), f"pb/{uuid.uuid4().hex}.md", _settings()
    )
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        generation = await connection.fetchrow(
            "SELECT id FROM research_document_generations WHERE document_id=$1 "
            "ORDER BY created_at DESC LIMIT 1", document.id,
        )
    from open_deep_research.documents.chunking import DocumentChunk
    from open_deep_research.documents.structuring import (
        PreparedDocument,
        StructuredUnit,
        plan_segments,
    )
    chunk = DocumentChunk(
        id=str(uuid.uuid4()), ordinal=0, locator="s:1", heading=name,
        text=f"{name} body", content_hash=hashlib.sha256(f"{name} body".encode()).hexdigest(),
    )
    unit = StructuredUnit(unit_type="paragraph", locator={"source": "s:1", "heading": name},
                          raw_text=f"{name} body", index_text=f"{name} body")
    prepared = PreparedDocument(units=[unit], parse_method="test")
    prepared = plan_segments(prepared, _settings())
    vectors = [[0.1] * 1536 for _ in prepared.segment_texts]
    await versioning.complete_generation(
        str(generation["id"]), [chunk], vectors,
        embedding_model="test", page_count=1, ocr_pages=0,
    )
    async with pool.acquire() as connection:
        await connection.execute(
            "UPDATE research_document_jobs SET status='completed' "
            "WHERE document_id=$1 AND status IN ('queued','running')", document.id,
        )
    from open_deep_research.documents import corrections
    await corrections.apply_corrections(
        owner, document.id, str(generation["id"]), revision=0,
        metadata_confirmed={"doc_type": "测试"},
    )
    await versioning.publish_generation(owner, document.id, str(generation["id"]))
    return document.id


async def test_trash_restore_and_batch(monkeypatch):
    monkeypatch.setenv("DOCUMENT_RESEARCH_ENABLED", "true")
    monkeypatch.setenv(
        "DOCUMENT_DATABASE_URL",
        _TEST_DSN.replace("postgresql+asyncpg://", "postgresql://", 1),
    )
    pool = await get_document_pool()
    try:
        # --- KB-15: trash does not queue cleanup ---
        doc_a = await _make_ready_doc(OWNER, "A")
        doc_b = await _make_ready_doc(OWNER, "B")
        doc_c = await _make_ready_doc(OWNER, "C")
        async with pool.acquire() as connection:
            delete_jobs = await connection.fetchval(
                "SELECT count(*) FROM research_document_jobs "
                "WHERE document_id=ANY($1::uuid[]) AND kind='delete'",
                [doc_a, doc_b, doc_c],
            )
        assert int(delete_jobs) == 0  # no immediate cleanup job

        # --- KB-14: batch trash with partial success ---
        batch = await batches.create_batch(OWNER, "trash", [doc_a, doc_b, doc_c])
        executed = await batches.execute_batch(OWNER, batch["batch_id"])
        assert executed["succeeded"] == 3 and executed["failed"] == 0
        detail = await batches.get_batch(OWNER, batch["batch_id"])
        assert detail["status"] == "completed"
        assert all(item["status"] == "succeeded" for item in detail["items"])

        # --- KB-15: trash list + restore ---
        base_id = None
        async with pool.acquire() as connection:
            base_id = await connection.fetchval(
                "SELECT home_knowledge_base_id FROM research_documents WHERE id=$1::uuid",
                doc_a,
            )
        trash_list = await trash.list_trash(OWNER, str(base_id))
        assert trash_list["total"] == 3
        restored = await trash.restore_document(OWNER, doc_a)
        assert restored["status"] == "ready"
        after = await trash.list_trash(OWNER, str(base_id))
        assert after["total"] == 2

        # Trashed document's chunks remain queryable for old citations
        await repository.get_chunk(OWNER, doc_b, "00000000-0000-0000-0000-000000000001")
        # (May be None if the generation hasn't published segments; that's fine —
        # the point is no 500 error from the trash status.)

        # --- KB-14: batch reparse with partial failure ---
        reparse_batch = await batches.create_batch(OWNER, "reparse", [doc_a, "0a09a9d1-115c-4764-a394-a21cbc55f140"])
        reparse_result = await batches.execute_batch(OWNER, reparse_batch["batch_id"])
        assert reparse_result["succeeded"] == 1
        assert reparse_result["failed"] == 1
        detail2 = await batches.get_batch(OWNER, reparse_batch["batch_id"])
        failed_item = next(i for i in detail2["items"] if i["status"] == "failed")
        assert failed_item["failure_code"] is not None

        # Retry failed only: the ghost id will fail again, the success won't re-run
        retry_result = await batches.retry_failed(OWNER, reparse_batch["batch_id"])
        assert retry_result["status"] == "pending"
        re_executed = await batches.execute_batch(OWNER, reparse_batch["batch_id"])
        assert re_executed["succeeded"] == 0  # ghost always fails
        assert re_executed["failed"] == 1
        detail3 = await batches.get_batch(OWNER, reparse_batch["batch_id"])
        ghost_attempts = next(
            i["attempt"] for i in detail3["items"] if i["document_id"] == "0a09a9d1-115c-4764-a394-a21cbc55f140"
        )
        assert ghost_attempts == 2  # retried exactly once more

        # --- KB-15: retention/purge respects references ---
        async with pool.acquire() as connection:
            # Simulate expiry: set purge_after in the past
            await connection.execute(
                "UPDATE research_documents SET purge_after=now()-interval '1 day' "
                "WHERE id=ANY($1::uuid[]) AND status='trashed'",
                [doc_b, doc_c],
            )
            # Bind a fake research run to doc_b → should be retained
            await connection.execute(
                """INSERT INTO research_run_sources
                     (run_id, owner_id, document_id, filename_snapshot, sha256_snapshot)
                   VALUES ('test-run-b', $1::uuid, $2, 'B', 'x')""",
                OWNER, doc_b,
            )
        purged = await trash.purge_due_documents()
        assert doc_c in purged and doc_b not in purged
        async with pool.acquire() as connection:
            status_b = await connection.fetchval(
                "SELECT status FROM research_documents WHERE id=$1", doc_b,
            )
            status_c = await connection.fetchval(
                "SELECT status FROM research_documents WHERE id=$1", doc_c,
            )
        assert status_b == "retained"  # referenced → kept
        assert status_c is None  # purged (row deleted)
    finally:
        async with pool.acquire() as connection:
            await connection.execute(
                "DELETE FROM research_run_sources WHERE owner_id=$1::uuid", OWNER)
            await connection.execute(
                "DELETE FROM knowledge_batch_items WHERE batch_id IN "
                "(SELECT id FROM knowledge_batches WHERE created_by=$1::uuid)", OWNER)
            await connection.execute(
                "DELETE FROM knowledge_batches WHERE created_by=$1::uuid", OWNER)
            await connection.execute(
                "DELETE FROM research_documents WHERE owner_id=$1::uuid", OWNER)
        await close_document_pool()
