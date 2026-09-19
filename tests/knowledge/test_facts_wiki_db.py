"""Phase-D+E: fact ledger, wiki, export (KB-12/KB-13/KB-15)."""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from tempfile import NamedTemporaryFile

import pytest

from open_deep_research.documents import repository, versioning
from open_deep_research.documents.database import close_document_pool, get_document_pool
from open_deep_research.documents.settings import (
    DocumentSettings,
    get_document_settings,
)
from open_deep_research.documents.storage import StagedUpload, resolve_storage_key
from open_deep_research.knowledge import authz, exporter, facts, wiki

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
        Path(handle.name),
        name,
        "text/markdown",
        len(content),
        hashlib.sha256(content).hexdigest(),
    )


def _settings():
    return DocumentSettings(max_documents_per_user=20, max_bytes_per_user=10**9)


async def _make_published_doc(owner: str, name: str, text: str) -> tuple[str, str]:
    key = f"pd/{uuid.uuid4().hex}.md"
    original = resolve_storage_key(key, get_document_settings())
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_text(text, encoding="utf-8")
    document, _ = await repository.create_document(
        owner, _staged(name, text.encode()), key, _settings()
    )
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        generation = await connection.fetchrow(
            "SELECT id FROM research_document_generations WHERE document_id=$1 "
            "ORDER BY created_at DESC LIMIT 1",
            document.id,
        )
    from open_deep_research.documents.chunking import DocumentChunk
    from open_deep_research.documents.structuring import (
        PreparedDocument,
        StructuredUnit,
        plan_segments,
    )

    chunk = DocumentChunk(
        id=str(uuid.uuid4()),
        ordinal=0,
        locator="s:1",
        heading=name,
        text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
    )
    unit = StructuredUnit(
        unit_type="paragraph",
        locator={"source": "s:1", "heading": name},
        raw_text=text,
        index_text=text,
    )
    prepared = PreparedDocument(units=[unit], parse_method="test")
    prepared = plan_segments(prepared, _settings())
    vectors = [[0.1] * 1536 for _ in prepared.segment_texts]
    await versioning.complete_generation(
        str(generation["id"]),
        [chunk],
        vectors,
        embedding_model="test",
        page_count=1,
        ocr_pages=0,
    )
    async with pool.acquire() as connection:
        await connection.execute(
            "UPDATE research_document_jobs SET status='completed' "
            "WHERE document_id=$1 AND status IN ('queued','running')",
            document.id,
        )
    from open_deep_research.documents import corrections

    await corrections.apply_corrections(
        owner,
        document.id,
        str(generation["id"]),
        revision=0,
        metadata_confirmed={"doc_type": "财报"},
    )
    await versioning.publish_generation(owner, document.id, str(generation["id"]))
    return document.id, str(generation["id"])


async def test_facts_wiki_and_export(monkeypatch):
    monkeypatch.setenv("DOCUMENT_RESEARCH_ENABLED", "true")
    monkeypatch.setenv(
        "DOCUMENT_DATABASE_URL",
        _TEST_DSN.replace("postgresql+asyncpg://", "postgresql://", 1),
    )
    pool = await get_document_pool()
    try:
        # Setup: publish one doc with financial data.
        text = "星澜科技2025财年营收48.6亿元，毛利率31.2%，市场份额5.8%"
        doc_id, gen_id = await _make_published_doc(OWNER, "finance.md", text)
        async with pool.acquire() as connection:
            kb_id = await connection.fetchval(
                "SELECT home_knowledge_base_id FROM research_documents WHERE id=$1",
                doc_id,
            )

        # --- KB-12: auto-extract candidates ---
        extracted = await facts.extract_candidates_for_generation(
            OWNER,
            str(kb_id),
            doc_id,
            gen_id,
        )
        assert len(extracted) >= 2  # 营收 + 毛利率 + 市场份额 at least
        metric_names = set()
        for item in extracted:
            listing = await facts.list_assertions(OWNER, str(kb_id), status="draft")
            for assertion in listing:
                metric_names.add(assertion["metric"])
        assert "营收" in metric_names
        assert "毛利率" in metric_names

        # Idempotent: re-running extraction for same generation produces nothing new.
        re_extracted = await facts.extract_candidates_for_generation(
            OWNER,
            str(kb_id),
            doc_id,
            gen_id,
        )
        assert re_extracted == []

        # --- KB-12: publish + conflicting values coexist ---
        # Publish the first candidate.
        all_drafts = await facts.list_assertions(OWNER, str(kb_id), status="draft")
        first_draft = next(a for a in all_drafts if a["metric"] == "营收")
        from open_deep_research.agentscope_runtime.knowledge import KnowledgeApplication

        async def authorize(command):
            assert command == "fact_publish"

        port = KnowledgeApplication(OWNER, authorize)
        tool = port.operation_tools(["fact_publish"])[0]
        published = (await tool.call(tool.input_schema(assertion_id=first_draft["id"]), None)).output
        assert published["status"] == "published"

        # Submit a conflicting value on the same key.
        conflicting = await facts.submit_assertion(
            OWNER,
            str(kb_id),
            entity_name=first_draft["entity_name"],
            metric=first_draft["metric"],
            value_text="营收49.1亿元",
            value_numeric="49.1",
            unit="元",
            scale="亿",
            raw_statement="不同来源的数据",
            period_label=first_draft["period_label"],
            data_period=first_draft["data_period"],
            evidence=[{"document_id": doc_id, "generation_id": gen_id}],
        )
        assert conflicting["status"] == "draft"
        # Both coexist on the same key.
        all_for_key = await facts.list_assertions(
            OWNER,
            str(kb_id),
            entity_name=first_draft["entity_name"],
            metric=first_draft["metric"],
        )
        statuses = {item["status"] for item in all_for_key}
        assert "published" in statuses and "draft" in statuses

        # --- KB-12: published assertion is immutable — change creates new record ---
        superseding = await facts.submit_assertion(
            OWNER,
            str(kb_id),
            entity_name=first_draft["entity_name"],
            metric=first_draft["metric"],
            value_text="营收50.0亿元",
            value_numeric="50.0",
            unit="元",
            scale="亿",
            period_label=first_draft["period_label"],
            data_period=first_draft["data_period"],
            evidence=[{"document_id": doc_id, "generation_id": gen_id}],
        )
        published_supersede = await facts.publish_assertion(
            OWNER,
            superseding["id"],
            supersedes_id=first_draft["id"],
        )
        assert published_supersede["status"] == "published"
        # The original is still there (not overwritten).
        still_there = await facts.list_assertions(
            OWNER,
            str(kb_id),
            entity_name=first_draft["entity_name"],
            metric=first_draft["metric"],
            status="published",
        )
        assert len(still_there) == 2  # original + superseding
        with pytest.raises(authz.AuthorizationError):
            await facts.publish_assertion(str(uuid.uuid4()), conflicting["id"])
        missing_source = await facts.submit_assertion(
            OWNER, str(kb_id), entity_name="测试", metric="价格", value_numeric="0"
        )
        with pytest.raises(facts.FactError, match="evidence_required"):
            await facts.publish_assertion(OWNER, missing_source["id"])
        assert (
            next(
                a
                for a in await facts.list_assertions(OWNER, str(kb_id))
                if a["id"] == missing_source["id"]
            )["value_numeric"]
            is not None
        )
        with pytest.raises(facts.FactError, match="evidence_not_published"):
            await facts.submit_assertion(
                OWNER,
                str(kb_id),
                entity_name="测试",
                metric="价格",
                evidence=[{"document_id": str(uuid.uuid4()), "generation_id": gen_id}],
            )
        await facts.review_value(OWNER, first_draft["id"], adopted=True)
        await facts.review_value(OWNER, superseding["id"], adopted=True)
        adopted = [
            a for a in await facts.list_assertions(OWNER, str(kb_id)) if a["adopted"]
        ]
        assert [a["id"] for a in adopted] == [superseding["id"]]

        # --- KB-13: wiki page lifecycle ---
        page = await wiki.create_page(
            OWNER,
            str(kb_id),
            title="星澜科技企业档案",
            template="company_profile",
            entity_name="星澜科技",
        )
        assert page["status"] == "draft"
        assert page["revision_number"] == 1

        # Read the template blocks.
        detail = await wiki.get_page(OWNER, page["id"])
        blocks = detail["current_revision"]["blocks"]
        assert len(blocks) >= 4

        # Edit: add citations to fact blocks.
        edited_blocks = []
        for block in blocks:
            if block.get("requires_source"):
                block["citations"] = [
                    {
                        "type": "document",
                        "document_id": doc_id,
                        "generation_id": gen_id,
                    }
                ]
                block["content"] += "\n星澜科技2025财年核心指标如上。"
            edited_blocks.append(block)

        draft2 = await wiki.save_draft(
            OWNER,
            page["id"],
            base_revision=1,
            blocks=edited_blocks,
        )
        assert draft2["revision_number"] == 2

        # Optimistic-lock conflict: stale base_revision.
        with pytest.raises(wiki.WikiError, match="revision_conflict"):
            await wiki.save_draft(
                OWNER, page["id"], base_revision=1, blocks=edited_blocks
            )

        # Publish (all fact blocks have citations → should succeed).
        published_page = await wiki.publish_page(OWNER, page["id"])
        assert published_page["status"] == "published"
        with pytest.raises(authz.AuthorizationError):
            await wiki.get_page(str(uuid.uuid4()), page["id"])
        # A later draft must not move the publication pointer.
        await wiki.save_draft(OWNER, page["id"], base_revision=2, blocks=edited_blocks)
        async with pool.acquire() as c:
            public_number = await c.fetchval(
                "SELECT r.revision_number FROM knowledge_pages p JOIN knowledge_page_revisions r ON r.id=p.published_revision_id WHERE p.id=$1::uuid",
                page["id"],
            )
        assert public_number == 2
        viewer = str(uuid.uuid4())
        async with pool.acquire() as c:
            workspace = await c.fetchval(
                "SELECT workspace_id FROM knowledge_bases WHERE id=$1", kb_id
            )
            await c.execute(
                "UPDATE knowledge_workspaces SET kind='team' WHERE id=$1", workspace
            )
            await c.execute(
                "INSERT INTO knowledge_workspace_members(workspace_id,user_id,role) VALUES($1,$2::uuid,'member')",
                workspace,
                viewer,
            )
            await c.execute(
                "INSERT INTO knowledge_base_members(knowledge_base_id,user_id,role) VALUES($1,$2::uuid,'viewer')",
                kb_id,
                viewer,
            )
        viewer_page = await wiki.get_page(viewer, page["id"], include_history=True)
        assert viewer_page["current_revision"]["number"] == 2
        assert all(h["status"] == "published" for h in viewer_page["history"])
        assert all(
            a["status"] == "published"
            for a in await facts.list_assertions(viewer, str(kb_id))
        )
        with pytest.raises(authz.AuthorizationError):
            await wiki.save_draft(
                viewer, page["id"], base_revision=3, blocks=edited_blocks
            )
        await wiki.generate_page(OWNER, page["id"], 3)
        generated = await wiki.get_page(OWNER, page["id"])
        generated_blocks = generated["current_revision"]["blocks"]
        manual_block = next(b for b in generated_blocks if b.get("generated_by"))
        manual_block["content"] = "人工保留的段落"
        await wiki.save_draft(
            OWNER, page["id"], base_revision=4, blocks=generated_blocks
        )
        await wiki.generate_page(OWNER, page["id"], 5)
        regenerated = await wiki.get_page(OWNER, page["id"])
        assert any(
            b["content"] == "人工保留的段落"
            for b in regenerated["current_revision"]["blocks"]
        )

        # Publish rejection: fact block without citations.
        unsourced_blocks = [b.copy() for b in edited_blocks]
        for block in unsourced_blocks:
            if block.get("requires_source"):
                block["citations"] = []
                break
        page2 = await wiki.create_page(
            OWNER, str(kb_id), title="Test", template="company_profile"
        )
        # Force the unsourced draft.
        await wiki.save_draft(
            OWNER, page2["id"], base_revision=1, blocks=unsourced_blocks
        )
        with pytest.raises(wiki.WikiError, match="missing_source"):
            await wiki.publish_page(OWNER, page2["id"])

        # --- KB-13: stale citation detection (before trash) ---
        stale = await wiki.check_stale_citations(page["id"])
        assert all(
            item["status"] == "source_changed" for item in stale
        )  # superseded fact is flagged

        # --- KB-15: export (before trash so the document is included) ---
        zip_bytes, filename = await exporter.export_knowledge_base(
            OWNER,
            str(kb_id),
        )
        assert filename.startswith("kb-export-")
        assert len(zip_bytes) > 0
        precheck = exporter.precheck_import(zip_bytes)
        assert precheck["ok"] is True
        assert precheck["document_count"] >= 1
        assert precheck["fact_count"] >= 1
        assert precheck["page_count"] >= 1
        with NamedTemporaryFile(suffix=".zip", delete_on_close=False) as transfer:
            transfer.close()
            Path(transfer.name).write_bytes(zip_bytes)
            imported = await exporter.import_archive(
                OWNER, str(kb_id), Path(transfer.name), str(uuid.uuid4())
            )
        assert imported["status"] == "review_required"
        imported_facts = await facts.list_assertions(
            OWNER, imported["knowledge_base_id"]
        )
        assert imported_facts and all(a["status"] == "draft" for a in imported_facts)

        # --- KB-13: stale citation after trash ---
        from open_deep_research.knowledge.trash import trash_document

        await trash_document(OWNER, doc_id)
        stale_after = await wiki.check_stale_citations(page["id"])
        assert any(item["status"] == "source_unavailable" for item in stale_after)
    finally:
        async with pool.acquire() as connection:
            await connection.execute(
                "UPDATE knowledge_pages SET current_revision_id=NULL,published_revision_id=NULL WHERE knowledge_base_id IN (SELECT id FROM knowledge_bases WHERE owner_id=$1::uuid)",
                OWNER,
            )
            for table in ("knowledge_jobs", "knowledge_editorial_audit"):
                await connection.execute(
                    f"DELETE FROM {table} WHERE knowledge_base_id IN (SELECT id FROM knowledge_bases WHERE owner_id=$1::uuid)",
                    OWNER,
                )
            await connection.execute(
                "DELETE FROM knowledge_page_citations WHERE revision_id IN "
                "(SELECT id FROM knowledge_page_revisions WHERE page_id IN "
                "(SELECT id FROM knowledge_pages WHERE knowledge_base_id IN "
                "(SELECT id FROM knowledge_bases WHERE owner_id=$1::uuid)))",
                OWNER,
            )
            await connection.execute(
                "DELETE FROM knowledge_page_revisions WHERE page_id IN "
                "(SELECT id FROM knowledge_pages WHERE knowledge_base_id IN "
                "(SELECT id FROM knowledge_bases WHERE owner_id=$1::uuid))",
                OWNER,
            )
            await connection.execute(
                "DELETE FROM knowledge_pages WHERE knowledge_base_id IN "
                "(SELECT id FROM knowledge_bases WHERE owner_id=$1::uuid)",
                OWNER,
            )
            await connection.execute(
                "DELETE FROM knowledge_fact_evidence WHERE assertion_id IN "
                "(SELECT id FROM knowledge_fact_assertions WHERE knowledge_base_id IN "
                "(SELECT id FROM knowledge_bases WHERE owner_id=$1::uuid))",
                OWNER,
            )
            await connection.execute(
                "DELETE FROM knowledge_fact_assertions WHERE knowledge_base_id IN "
                "(SELECT id FROM knowledge_bases WHERE owner_id=$1::uuid)",
                OWNER,
            )
            await connection.execute(
                "DELETE FROM knowledge_fact_keys WHERE id NOT IN "
                "(SELECT DISTINCT fact_key_id FROM knowledge_fact_assertions)"
            )
            await connection.execute(
                "DELETE FROM knowledge_sync_sources WHERE document_id IN "
                "(SELECT id FROM research_documents WHERE owner_id=$1::uuid)",
                OWNER,
            )
            await connection.execute(
                "DELETE FROM knowledge_content_fingerprints WHERE document_id IN "
                "(SELECT id FROM research_documents WHERE owner_id=$1::uuid)",
                OWNER,
            )
            await connection.execute(
                "DELETE FROM knowledge_source_relations WHERE left_document_id IN "
                "(SELECT id FROM research_documents WHERE owner_id=$1::uuid)",
                OWNER,
            )
            await connection.execute(
                "DELETE FROM research_run_sources WHERE owner_id=$1::uuid", OWNER
            )
            await connection.execute(
                "DELETE FROM research_documents WHERE owner_id=$1::uuid", OWNER
            )
            await connection.execute(
                "DELETE FROM knowledge_bases WHERE owner_id=$1::uuid", OWNER
            )
        await close_document_pool()
