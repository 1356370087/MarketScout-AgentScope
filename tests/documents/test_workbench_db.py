"""Phase-4 workbench flow against real PostgreSQL and Docling Serve.

Requires ``IAM_TEST_DATABASE_URL`` plus ``DOCLING_SERVE_URL`` /
``DOCLING_SERVE_API_KEY``; skipped otherwise. Covers the completion
condition: corrections stay traceable (audit + revision lock) and published
generations never change under review work (plan §5.1 stage 4).
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from tempfile import NamedTemporaryFile

import pytest

from open_deep_research.documents import (
    corrections,
    reparse,
    repository,
    retrieval,
    structuring,
    versioning,
)
from open_deep_research.documents.parse_pipeline import parse_document_structured
from open_deep_research.documents.settings import get_document_settings
from open_deep_research.documents.storage import StagedUpload
from open_deep_research.knowledge import search_service

_TEST_DSN = os.environ.get("IAM_TEST_DATABASE_URL", "")
_DOCLING = os.environ.get("DOCLING_SERVE_URL", "").strip()
_DOCLING_KEY = os.environ.get("DOCLING_SERVE_API_KEY", "").strip()

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.db,
    pytest.mark.skipif(
        not _TEST_DSN or not _DOCLING or not _DOCLING_KEY,
        reason="IAM_TEST_DATABASE_URL and DOCLING_SERVE_URL/API_KEY required",
    ),
]

VEC = [0.1] * 1536
OWNER = str(uuid.uuid4())


async def _fake_embed(texts, settings=None, **_kwargs):
    return [VEC for _ in texts]


def _staged(name: str, content: bytes, media: str) -> StagedUpload:
    handle = NamedTemporaryFile(delete=False, suffix=Path(name).suffix)
    handle.write(content)
    handle.close()
    return StagedUpload(
        Path(handle.name), name, media, len(content),
        hashlib.sha256(content).hexdigest(),
    )


def _two_page_pdf(path: Path) -> Path:
    import fitz

    font = next(
        (p for p in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simsun.ttc") if os.path.exists(p)),
        None,
    )
    document = fitz.open()
    page_one = document.new_page()
    if font:
        page_one.insert_font(fontname="cjk", fontfile=font)
        fontname = "cjk"
    else:
        fontname = "helv"
    page_one.insert_text((72, 90), "Starline FY2025 Financial Statements", fontsize=16, fontname=fontname)
    page_one.insert_text((72, 130), "Publication date: 2026-03-31", fontsize=10, fontname=fontname)
    page_one.insert_text((72, 160), "Item    FY2025", fontsize=10, fontname=fontname)
    page_one.insert_text((72, 178), "Revenue 4,860,000", fontsize=10, fontname=fontname)
    page_two = document.new_page()
    page_two.insert_font(fontname="cjk", fontfile=font) if font else None
    page_two.insert_text(
        (72, 90), "Notes: figures restated per audit adjustment notice.",
        fontsize=10, fontname=fontname,
    )
    document.save(path)
    document.close()
    return path


async def test_workbench_corrections_reparse_and_diff(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCUMENT_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("LITELLM_SERVICE_KEY", "fixture-service")
    monkeypatch.setenv(
        "DOCUMENT_DATABASE_URL",
        _TEST_DSN.replace("postgresql+asyncpg://", "postgresql://", 1),
    )
    monkeypatch.setenv("DOCLING_SERVE_URL", _DOCLING)
    monkeypatch.setenv("DOCLING_SERVE_API_KEY", _DOCLING_KEY)
    monkeypatch.setenv("DOCLING_TIMEOUT_SECONDS", os.environ.get("DOCLING_TIMEOUT_SECONDS", "600"))
    monkeypatch.setenv("DOCUMENT_STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("LITELLM_SERVICE_KEY", "fixture-service")
    settings = get_document_settings()
    monkeypatch.setattr(search_service, "embed_texts", _fake_embed)
    monkeypatch.setattr(corrections, "embed_texts", _fake_embed)
    monkeypatch.setattr(reparse, "embed_texts", _fake_embed)

    from open_deep_research.documents.database import get_document_pool

    pool = await get_document_pool()

    # --- publish the baseline through the real Docling service ---
    pdf_path = _two_page_pdf(tmp_path / "two-page.pdf")
    stored_original = settings.storage_dir / "workbench" / "report.pdf"
    stored_original.parent.mkdir(parents=True, exist_ok=True)
    stored_original.write_bytes(pdf_path.read_bytes())
    document, deduplicated = await repository.create_document(
        OWNER,
        StagedUpload(pdf_path, "two-page.pdf", "application/pdf",
                     pdf_path.stat().st_size, hashlib.sha256(pdf_path.read_bytes()).hexdigest()),
        "workbench/report.pdf",
        settings,
    )
    assert not deduplicated
    doc_id = document.id
    async with pool.acquire() as connection:
        generation = await connection.fetchrow(
            "SELECT * FROM research_document_generations WHERE document_id=$1 "
            "ORDER BY created_at DESC LIMIT 1", doc_id,
        )
    prepared = await parse_document_structured(
        pdf_path, "two-page.pdf", "application/pdf", settings,
        version_id=str(generation["version_id"]),
    )
    prepared = structuring.plan_segments(prepared, settings)
    vectors = await _fake_embed(prepared.segment_texts)
    await versioning.complete_generation_rich(
        str(generation["id"]), prepared, vectors,
        embedding_model=settings.embedding_model,
        metadata_suggestions={"candidates": {"doc_type": {"value": "财报"}}},
    )
    async with pool.acquire() as connection:
        await connection.execute(
            "UPDATE research_document_jobs SET status='completed' "
            "WHERE document_id=$1 AND status IN ('queued','running')", doc_id,
        )
    await corrections.apply_corrections(
        OWNER, doc_id, str(generation["id"]), revision=0,
        metadata_confirmed={"doc_type": "财报"},
    )
    await versioning.publish_generation(OWNER, doc_id, str(generation["id"]))
    published_units = await _units(pool, str(generation["id"]))
    assert any(int(unit["locator"]["page"] or 0) == 2 for unit in published_units)
    baseline_hits = await retrieval.search_document_chunks(
        owner_id=OWNER, document_ids=[doc_id], query="Revenue restated")
    assert baseline_hits

    # --- scoped re-parse of page 2 copies everything, replaces page 2 ---
    draft_id = await reparse.queue_scoped_reparse(
        OWNER, doc_id, str(generation["id"]), pages=[2], reason="审计调整后重读附注",
    )
    async with pool.acquire() as connection:
        copied_segments = await connection.fetchval(
            "SELECT count(*) FROM research_document_segments WHERE generation_id=$1",
            draft_id,
        )
        copied_vectors = await connection.fetchval(
            "SELECT count(*) FROM research_document_segments "
            "WHERE generation_id=$1 AND embedding IS NOT NULL",
            draft_id,
        )
        assert copied_segments == copied_vectors > 0  # vectors reused verbatim
        draft_row = await connection.fetchrow(
            "SELECT * FROM research_document_generations WHERE id=$1", draft_id,
        )
    await reparse.execute_scoped_reparse(
        {"id": doc_id, "storage_key": "workbench/report.pdf",
         "filename": "two-page.pdf", "media_type": "application/pdf"},
        dict(draft_row), settings,
    )
    draft_units = await _units(pool, draft_id)
    page_one_old = [u for u in published_units if int(u["locator"].get("page") or 0) == 1]
    page_one_new = [u for u in draft_units if int(u["locator"].get("page") or 0) == 1]
    # Out-of-scope pages keep their content verbatim, though the copied
    # units carry fresh identities (plan §2.4).
    assert {(u["locator"]["source"], u["raw_text"]) for u in page_one_old} == {
        (u["locator"]["source"], u["raw_text"]) for u in page_one_new
    }
    assert page_one_new and all(
        str(u["id"]) != str(old["id"]) for u in page_one_new for old in page_one_old
    )
    review = await versioning.generation_review_detail(OWNER, doc_id, draft_id)
    assert review["status"] == "pending_review"
    assert any("scoped_reparse" in flag for flag in review["quality_report"].get("flags", []))

    # --- corrections with revision lock: edit, stale conflict, audit ---
    target = page_one_new[0]["id"] if page_one_new else draft_units[0]["id"]
    edited = await corrections.apply_corrections(
        OWNER, doc_id, draft_id, revision=0,
        unit_corrections=[{"unit_id": target, "revised_text": "人工修订后的标题内容"}],
        metadata_confirmed={"doc_type": "财报", "publish_date": "未知"},
    )
    assert edited["revision"] == 1
    with pytest.raises(corrections.CorrectionRevisionError):
        await corrections.apply_corrections(
            OWNER, doc_id, draft_id, revision=0, metadata_confirmed={"x": "y"},
        )
    # Segment content now serves the revision inside the draft.
    async with pool.acquire() as connection:
        served = await connection.fetchval(
            "SELECT index_text FROM research_document_segments WHERE unit_id=$1 LIMIT 1",
            target,
        )
    assert served and "人工修订后的标题内容" in served

    # A056: the unpublished correction cannot leak into current retrieval.
    current_hits = await retrieval.search_document_chunks(
        owner_id=OWNER, document_ids=[doc_id], query="Revenue restated")
    assert current_hits
    assert all("人工修订后的标题内容" not in hit["text"] for hit in current_hits)

    # Exercise HTTP status mapping against the real conflicting revision.
    import httpx
    from fastapi import FastAPI

    from open_deep_research.documents import router as document_routes
    from open_deep_research.documents.database import initialize_document_schema
    from security.rbac.principal import Principal

    assert await initialize_document_schema() is None
    principal = Principal(user_id=OWNER, email="owner@test.invalid", status="active",
                          session_id=None, roles=frozenset(), permissions=frozenset(),
                          authz_version=0)
    app = FastAPI()
    app.include_router(document_routes.router)
    route = next(r for r in document_routes.router.routes if getattr(r, "endpoint", None) is document_routes.apply_generation_corrections)
    for dependency in route.dependant.dependencies:
        if dependency.name == "user":
            app.dependency_overrides[dependency.call] = lambda: principal
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.post(
            f"/documents/{doc_id}/generations/{draft_id}/corrections",
            json={"revision": 0, "metadata_confirmed": {"x": "y"}},
        )
    assert response.status_code == 409, response.text

    # --- diff against the published baseline shows the edit ---
    diff = await versioning.diff_generations(OWNER, doc_id, draft_id)
    assert diff["base"]["id"] == str(generation["id"])
    assert any("人工修订后的标题内容" in item["text"] for item in diff["units"]["changed"])
    assert diff["metadata"]["doc_type"]["confirmed"] == "财报"

    # --- publish the draft; the old published generation stays intact ---
    old_units = await _units(pool, str(generation["id"]))
    await versioning.publish_generation(OWNER, doc_id, draft_id)
    assert await _units(pool, str(generation["id"])) == old_units  # immutability
    hits = await retrieval.search_document_chunks(
        owner_id=OWNER, document_ids=[doc_id], query="Revenue restated 人工修订")
    assert hits and any("人工修订后的标题内容" in hit["text"] for hit in hits)

    async with pool.acquire() as connection:
        operations = [
            row["operation"]
            for row in await connection.fetch(
                "SELECT operation FROM research_document_operations "
                "WHERE owner_id=$1::uuid ORDER BY created_at", OWNER,
            )
        ]
        await connection.execute(
            "DELETE FROM research_run_sources WHERE owner_id=$1::uuid", OWNER)
        await connection.execute(
            "DELETE FROM research_documents WHERE owner_id=$1::uuid", OWNER)
    assert "correct" in operations and "queue_scoped_reparse" in operations
    assert operations.count("publish") == 2
    from open_deep_research.documents.database import close_document_pool

    await close_document_pool()


async def _units(pool, generation_id: str) -> list[dict]:
    from open_deep_research.documents.retrieval import locator_dict

    async with pool.acquire() as connection:
        rows = await connection.fetch(
            "SELECT * FROM research_document_units WHERE generation_id=$1::uuid ORDER BY ordinal",
            generation_id,
        )
    units = []
    for row in rows:
        unit = dict(row)
        unit["locator"] = locator_dict(unit["locator"])
        units.append(unit)
    return units
