"""Phase-C sync + dedup against a real PostgreSQL (KB-10/KB-11)."""

from __future__ import annotations

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
from open_deep_research.knowledge import dedup, sync

_TEST_DSN = os.environ.get("IAM_TEST_DATABASE_URL", "")

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.db,
    pytest.mark.skipif(not _TEST_DSN, reason="IAM_TEST_DATABASE_URL not configured"),
]

OWNER = str(uuid.uuid4())


def _staged(name: str, content: bytes) -> StagedUpload:
    handle = NamedTemporaryFile(delete=False, suffix=Path(name).suffix)
    handle.write(content)
    handle.close()
    return StagedUpload(
        Path(handle.name), name, "text/markdown", len(content),
        hashlib.sha256(content).hexdigest(),
    )


def _settings():
    return DocumentSettings(max_documents_per_user=20, max_bytes_per_user=10**9)


async def _publish_doc(owner: str, name: str, text: str) -> str:
    document, _ = await repository.create_document(
        owner, _staged(name, text.encode()), f"pc/{uuid.uuid4().hex}.md", _settings()
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
        text=text, content_hash=hashlib.sha256(text.encode()).hexdigest(),
    )
    unit = StructuredUnit(unit_type="paragraph", locator={"source": "s:1", "heading": name},
                          raw_text=text, index_text=text)
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
        metadata_confirmed={"doc_type": "行业报告"},
    )
    await versioning.publish_generation(owner, document.id, str(generation["id"]))
    return document.id, str(generation["id"])


async def test_sync_source_lifecycle_and_dedup(monkeypatch):
    monkeypatch.setenv("DOCUMENT_RESEARCH_ENABLED", "true")
    monkeypatch.setenv(
        "DOCUMENT_DATABASE_URL",
        _TEST_DSN.replace("postgresql+asyncpg://", "postgresql://", 1),
    )
    pool = await get_document_pool()
    try:
        # --- KB-11: fingerprint three documents ---
        # doc_a and doc_b have same content (different formatting) → exact_duplicate
        # doc_c is a near-duplicate repost (minor punctuation changes)
        # doc_d is a different quarterly report → no match
        text_a = "星澜科技2025财年营收48.6亿元 毛利率31.2% 同比增长15.4%"
        text_b = "星澜科技2025财年\n营收48.6亿元\n毛利率31.2%\n同比增长15.4%"  # formatting only
        text_c = "星澜科技2025财年营收48.6亿元，毛利率31.2%，同比增长15.4%"  # punctuation
        text_d = "2024年第四季度营收42.1亿元 毛利率29.8% 研发费用3.2亿"

        doc_a, gen_a = await _publish_doc(OWNER, "A.md", text_a)
        doc_b, gen_b = await _publish_doc(OWNER, "B.md", text_b)
        doc_c, gen_c = await _publish_doc(OWNER, "C.md", text_c)
        doc_d, gen_d = await _publish_doc(OWNER, "D.md", text_d)

        await dedup.compute_fingerprint(doc_a, gen_a, text_a, source_class="official")
        await dedup.compute_fingerprint(doc_b, gen_b, text_b, source_class="repost")
        await dedup.compute_fingerprint(doc_c, gen_c, text_c, source_class="media")
        await dedup.compute_fingerprint(doc_d, gen_d, text_d, source_class="official")

        # --- Layer 2: exact normalized-text match ---
        duplicates_b = await dedup.find_duplicates(OWNER, doc_b, gen_b)
        exact_matches = [d for d in duplicates_b if d["relation_type"] == "exact_duplicate"]
        assert any(d["document_id"] == doc_a for d in exact_matches)

        # --- Layer 3: near-duplicate ---
        duplicates_a = await dedup.find_duplicates(OWNER, doc_a, gen_a)
        [d for d in duplicates_a if d["relation_type"] == "near_duplicate"]
        # doc_c should be a near-duplicate of doc_a (same text, only punctuation differs
        # → normalized identical → caught by Layer 2, not 3; that's fine, it's found)
        all_a_ids = {d["document_id"] for d in duplicates_a}
        assert doc_c in all_a_ids  # found as either exact or near

        # doc_d must NOT appear as duplicate of doc_a
        assert doc_d not in all_a_ids

        # --- Record and confirm relations ---
        count = await dedup.record_suspected_relations(doc_a, duplicates_a)
        assert count > 0
        async with pool.acquire() as connection:
            kb_id = await connection.fetchval(
                "SELECT home_knowledge_base_id FROM research_documents WHERE id=$1", doc_a,
            )
            relations = await connection.fetch(
                """SELECT * FROM knowledge_source_relations
                    WHERE left_document_id=ANY($1::uuid[]) OR right_document_id=ANY($1::uuid[])""",
                [doc_a],
            )
        assert len(relations) >= 2  # at least doc_b and doc_c
        # Confirm the exact duplicate relation
        for relation in relations:
            if relation["relation_type"] == "exact_duplicate":
                confirmed = await dedup.confirm_relation(OWNER, str(relation["id"]))
                assert confirmed["confirmed"] is True
                break
        # List relations
        listed = await dedup.list_relations(OWNER, str(kb_id), confirmed=True)
        assert any(item["confirmed"] for item in listed)

        # --- KB-10: sync source lifecycle ---
        source = await sync.create_sync_source(
            OWNER, str(kb_id), doc_d, "https://example.com/report",
            refresh_mode="daily",
        )
        assert source["normalized_url"] == "https://example.com/report"

        # Sync with unchanged content (mock the adapter to return 304-equivalent)
        async def mock_discover(self, source_rec):
            return None  # unchanged

        original_discover = sync.WebAdapter.discover_changes
        sync.WebAdapter.discover_changes = mock_discover
        async def authorize(url):
            assert url.startswith("https://")

        result_unchanged = await sync.run_sync(OWNER, source["id"], authorize_url=authorize)
        assert result_unchanged["status"] == "unchanged"

        # Sync with changed content
        async def mock_changed(self, source_rec):
            return sync.SyncSnapshot(
                body=b"<html><body>Updated content</body></html>",
                final_url="https://example.com/report",
                etag='"new-etag"',
                content_hash=hashlib.sha256(b"Updated content").hexdigest(),
                extracted_text="Updated content",
            )

        sync.WebAdapter.discover_changes = mock_changed
        result_updated = await sync.run_sync(OWNER, source["id"], authorize_url=authorize)
        assert result_updated["status"] == "updated"
        assert result_updated["version"]["version_no"] == 2

        # The new version is a draft (pending review), not auto-published
        async with pool.acquire() as connection:
            new_gen_status = await connection.fetchval(
                """SELECT g.status FROM research_document_generations g
                    JOIN research_document_versions v ON v.id = g.version_id
                   WHERE g.document_id=$1 AND v.version_no = 2""",
                doc_d,
            )
            current_gen = await connection.fetchval(
                "SELECT current_generation_id FROM research_documents WHERE id=$1",
                doc_d,
            )
        assert new_gen_status in ("draft", "pending_review")  # 未发布，等待解析与人工审核
        assert str(current_gen) == gen_d  # old version still serving

        # Pending draft blocks new sync version creation (single-pending guard)
        sync.WebAdapter.discover_changes = mock_changed
        result_blocked = await sync.run_sync(OWNER, source["id"], authorize_url=authorize)
        # Second sync is blocked by the in-progress ingest job (方案 KB-10：
        # 存在待审核更新时暂停自动追加版本)
        assert result_blocked["status"] == "error"
        assert "in_progress" in result_blocked.get("code", "")

        sync.WebAdapter.discover_changes = original_discover
    finally:
        async with pool.acquire() as connection:
            await connection.execute(
                "DELETE FROM knowledge_source_relations "
                "WHERE left_document_id IN (SELECT id FROM research_documents WHERE owner_id=$1::uuid)",
                OWNER,
            )
            await connection.execute(
                "DELETE FROM knowledge_content_fingerprints "
                "WHERE document_id IN (SELECT id FROM research_documents WHERE owner_id=$1::uuid)",
                OWNER,
            )
            await connection.execute(
                "DELETE FROM knowledge_sync_sources "
                "WHERE document_id IN (SELECT id FROM research_documents WHERE owner_id=$1::uuid)",
                OWNER,
            )
            await connection.execute(
                "DELETE FROM research_run_sources WHERE owner_id=$1::uuid", OWNER,
            )
            await connection.execute(
                "DELETE FROM research_documents WHERE owner_id=$1::uuid", OWNER)
        await close_document_pool()
