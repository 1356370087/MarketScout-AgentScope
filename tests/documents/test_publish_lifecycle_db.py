"""Phase-2 publish lifecycle against a real PostgreSQL (plan §5.1 stage 2).

Requires ``IAM_TEST_DATABASE_URL``; skipped otherwise. Exercises the full
sequence: upload → parse completion → pending_review gate → publish →
revision upload → pointer move → frozen run binding → historical citations →
withdraw → reject → selection gating → audit trail.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

import pytest

from open_deep_research.documents import repository, retrieval, versioning
from open_deep_research.documents.chunking import DocumentChunk
from open_deep_research.documents.settings import get_document_settings
from open_deep_research.documents.storage import StagedUpload

_TEST_DSN = os.environ.get("IAM_TEST_DATABASE_URL", "")

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.db,
    pytest.mark.skipif(not _TEST_DSN, reason="IAM_TEST_DATABASE_URL not configured"),
]

VEC = [0.1] * 1536


def _staged(name: str, content: bytes) -> StagedUpload:
    return StagedUpload(
        Path(name), name, "text/markdown", len(content),
        hashlib.sha256(content).hexdigest(),
    )


def _chunk(ordinal: int, heading: str, text: str) -> DocumentChunk:
    return DocumentChunk(
        id=str(uuid.uuid4()), ordinal=ordinal, locator=f"section-{ordinal}",
        heading=heading, text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
    )


async def _search(owner: str, doc_id: str, *, run_id: str | None = None):
    return await retrieval.search_document_chunks(
        owner_id=owner, document_ids=[doc_id], query="定价 单价", run_id=run_id,
    )


async def _finish_jobs(doc_id: str) -> None:
    """Mirror what the real worker's complete_job does to the durable queue."""
    from open_deep_research.documents.database import get_document_pool

    pool = await get_document_pool()
    async with pool.acquire() as connection:
        await connection.execute(
            "UPDATE research_document_jobs SET status='completed' "
            "WHERE document_id=$1::uuid AND status IN ('queued','running')",
            doc_id,
        )


async def test_publish_lifecycle_gates_retrieval_and_freezes_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = str(uuid.uuid4())
    # The document pool reads DocumentSettings; the root conftest strips
    # project-dotenv keys during tests, so point it at the test DSN here.
    monkeypatch.setenv("DOCUMENT_RESEARCH_ENABLED", "true")
    monkeypatch.setenv(
        "DOCUMENT_DATABASE_URL",
        _TEST_DSN.replace("postgresql+asyncpg://", "postgresql://", 1),
    )

    async def fake_embed(texts, settings, **_kwargs):
        return [VEC for _ in texts]

    monkeypatch.setattr(retrieval, "embed_texts", fake_embed)
    settings = get_document_settings()
    document, deduplicated = await repository.create_document(
        owner,
        _staged("kb-lifecycle-v1.md", b"# v1\nalpha pricing table\n"),
        "lifecycle/aa/blob.md",
        settings,
    )
    assert not deduplicated
    doc_id = document.id

    generations = await versioning.list_generations(owner, doc_id)
    assert len(generations) == 1 and generations[0]["status"] == "draft"
    generation_v1 = generations[0]["id"]

    await versioning.complete_generation(
        generation_v1,
        [
            _chunk(0, "定价", "v1: 标准授权单价 12000 元，有效期至 2025-12-31"),
            _chunk(1, "概述", "v1: 星澜科技 2025 财年营收 48.6 亿元"),
        ],
        [VEC, VEC],
        embedding_model="if-embedding-v1",
        page_count=2,
        ocr_pages=0,
    )
    detail = await versioning.generation_detail(owner, doc_id, generation_v1)
    assert detail["status"] == "pending_review"
    await _finish_jobs(doc_id)

    # Pending generations must be invisible to retrieval.
    assert await _search(owner, doc_id) == []

    # A publish now requires explicit metadata confirmation (plan §2.3).
    with pytest.raises(
        repository.DocumentConflictError, match="metadata_not_confirmed"
    ):
        await versioning.publish_generation(owner, doc_id, generation_v1)
    from open_deep_research.documents import corrections as corrections_module

    confirmed = await corrections_module.apply_corrections(
        owner, doc_id, generation_v1, revision=0,
        metadata_confirmed={"doc_type": "财报", "period": "未知"},
    )
    assert confirmed["revision"] == 1

    await versioning.publish_generation(owner, doc_id, generation_v1)
    hits = await _search(owner, doc_id)
    texts = [item["text"] for item in hits]
    assert len(hits) == 2 and any("12000" in text for text in texts)
    v1_segment = next(item["chunk_id"] for item in hits if "12000" in item["text"])
    # Re-publishing the current generation is an idempotent no-op.
    assert (await versioning.publish_generation(owner, doc_id, generation_v1))[
        "is_current"
    ]

    from open_deep_research.documents.database import (
        close_document_pool,
        get_document_pool,
    )

    pool = await get_document_pool()
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT * FROM research_documents WHERE id=$1::uuid", doc_id
        )
    await repository.bind_run_sources("run-lifecycle", owner, [row])

    revision = await versioning.add_document_version(
        owner,
        doc_id,
        _staged("kb-lifecycle-v2.md", b"# v2\nalpha pricing revised\n"),
        "lifecycle/bb/blob.md",
        note="价格更新",
    )
    generation_v2 = revision["generation"]["id"]
    versions = await versioning.list_versions(owner, doc_id)
    assert [item["version_no"] for item in versions] == [2, 1]

    await versioning.complete_generation(
        generation_v2,
        [_chunk(0, "定价", "v2: 标准授权单价 15000 元，有效期至 2026-12-31")],
        [VEC],
        embedding_model="if-embedding-v1",
        page_count=2,
        ocr_pages=0,
    )
    await _finish_jobs(doc_id)
    # A pending revision never disturbs the published version.
    texts = [item["text"] for item in await _search(owner, doc_id)]
    assert any("12000" in text for text in texts) and not any(
        "15000" in text for text in texts
    )

    await corrections_module.apply_corrections(
        owner, doc_id, generation_v2, revision=0, metadata_confirmed={"doc_type": "价格表"}
    )
    await versioning.publish_generation(owner, doc_id, generation_v2)
    texts = [item["text"] for item in await _search(owner, doc_id)]
    assert texts and all("15000" in text for text in texts)

    # A run that bound v1 stays frozen on v1 after v2 publishes.
    frozen = [item["text"] for item in await _search(owner, doc_id, run_id="run-lifecycle")]
    assert any("12000" in text for text in frozen) and not any(
        "15000" in text for text in frozen
    )

    # Historical v1 citations keep resolving after the pointer moved.
    legacy = await repository.get_chunk(owner, doc_id, v1_segment)
    assert legacy is not None and "12000" in legacy.text

    withdraw = await versioning.withdraw_version(
        owner, doc_id, revision["version"]["id"], reason="错误版本"
    )
    assert withdraw["cleared_current_pointer"] is True
    assert await _search(owner, doc_id) == []
    assert await repository.get_chunk(owner, doc_id, v1_segment) is not None

    # Withdrawn material cannot start new runs (still 'ready', not published).
    from open_deep_research.documents.contracts import SourceSelection

    with pytest.raises(
        repository.DocumentConflictError, match="document_not_published"
    ):
        await repository.validate_selection(
            owner,
            SourceSelection(mode="documents", sources=[{"type": "document", "id": doc_id}]),
        )

    rejected = await versioning.reject_generation(
        owner,
        doc_id,
        await versioning.queue_reindex_generation(owner, doc_id),
        reason="解析质量差",
    )
    assert rejected["status"] == "rejected"
    with pytest.raises(repository.DocumentConflictError):
        await versioning.publish_generation(owner, doc_id, rejected["id"])

    async with pool.acquire() as connection:
        operations = [
            record["operation"]
            for record in await connection.fetch(
                "SELECT operation FROM research_document_operations "
                "WHERE owner_id=$1::uuid ORDER BY created_at",
                owner,
            )
        ]
        await connection.execute(
            "DELETE FROM research_run_sources WHERE owner_id=$1::uuid", owner
        )
        await connection.execute(
            "DELETE FROM research_documents WHERE owner_id=$1::uuid", owner
        )
    assert operations == [
        "upload_version", "correct", "publish", "upload_version", "correct",
        "publish", "withdraw", "queue_reparse", "reject",
    ]
    await close_document_pool()
