"""Stage-5/6 live flow: unified search, Q&A, KB source expansion, evaluation.

Runs against a real PostgreSQL (``IAM_TEST_DATABASE_URL``); skipped
otherwise. Embedding, rerank and the answer model are faked so the test
exercises the pipeline, gates and ledgers without external model spend.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from tempfile import NamedTemporaryFile

import pytest

from open_deep_research.documents import corrections, repository, versioning
from open_deep_research.documents.contracts import SourceSelection
from open_deep_research.documents.database import get_document_pool
from open_deep_research.documents.storage import StagedUpload
from open_deep_research.knowledge import answer, evaluation, search_service
from open_deep_research.knowledge.rerank import RerankUnavailableError

_TEST_DSN = os.environ.get("IAM_TEST_DATABASE_URL", "")

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.db,
    pytest.mark.skipif(not _TEST_DSN, reason="IAM_TEST_DATABASE_URL not configured"),
]

VEC = [0.1] * 1536
OWNER = str(uuid.uuid4())


def _staged(name: str, content: bytes, media: str) -> StagedUpload:
    handle = NamedTemporaryFile(delete=False, suffix=Path(name).suffix)
    handle.write(content)
    handle.close()
    return StagedUpload(
        Path(handle.name), name, media, len(content),
        hashlib.sha256(content).hexdigest(),
    )


async def _fake_embed(texts, settings=None, **_kwargs):
    return [VEC for _ in texts]


async def _seed(pool) -> dict:
    """Seed two published docs, a KB/collection and one entity alias."""
    async def make_doc(filename: str, chunks: list[tuple[str, str]], metadata: dict):
        from open_deep_research.documents.settings import DocumentSettings

        document, dedup = await repository.create_document(
            OWNER,
            _staged(filename, filename.encode(), "text/markdown"),
            f"p5/{filename}",
            DocumentSettings(max_documents_per_user=10, max_bytes_per_user=10**9),
        )
        assert not dedup
        async with pool.acquire() as connection:
            generation = await connection.fetchrow(
                "SELECT * FROM research_document_generations WHERE document_id=$1 "
                "ORDER BY created_at DESC LIMIT 1", document.id,
            )
        prepared_units = []
        for index, (heading, text) in enumerate(chunks):
            from open_deep_research.documents.structuring import StructuredUnit
            prepared_units.append(
                StructuredUnit(
                    unit_type="paragraph",
                    locator={"source": f"section:{index + 1}", "heading": heading},
                    raw_text=text,
                    index_text=f"{heading}\n{text}",
                )
            )
        from open_deep_research.documents.structuring import (
            PreparedDocument,
            plan_segments,
        )
        prepared = PreparedDocument(units=prepared_units, parse_method="test")
        prepared = plan_segments(prepared, _settings_stub())
        vectors = await _fake_embed(prepared.segment_texts)
        await versioning.complete_generation_rich(
            str(generation["id"]), prepared, vectors,
            embedding_model="if-embedding-v1",
            metadata_suggestions=metadata,
        )
        async with pool.acquire() as connection:
            await connection.execute(
                "UPDATE research_document_jobs SET status='completed' "
                "WHERE document_id=$1 AND status IN ('queued','running')", document.id,
            )
        await corrections.apply_corrections(
            OWNER, document.id, str(generation["id"]), revision=0,
            metadata_confirmed={"doc_type": metadata["candidates"]["doc_type"]["value"]},
        )
        await versioning.publish_generation(OWNER, document.id, str(generation["id"]))
        return document, str(generation["id"])

    finance, finance_gen = await make_doc(
        "FY2025-report.md",
        [
            ("营收", "星澜科技 2025 财年营业收入 48.6 亿元，同比增长 15.4%。"),
            ("毛利率", "毛利率 31.2%，较上年提升 1.4 个百分点。"),
        ],
        {"candidates": {"doc_type": {"value": "财报"}}},
    )
    pricing, pricing_gen = await make_doc(
        "HX-price.md",
        [
            ("定价", "HX-200 控制器单价 15000 元，有效期 2025-06-01 至 2026-05-31。"),
            ("说明", "价格含税，不含安装服务。"),
        ],
        {"candidates": {"doc_type": {"value": "价格表"}}},
    )
    async with pool.acquire() as connection:
        entity_id = await connection.fetchval(
            "INSERT INTO knowledge_entities(owner_id, entity_kind, name_zh) "
            "VALUES ($1::uuid,'company','星澜科技') RETURNING id", OWNER,
        )
        await connection.execute(
            "INSERT INTO knowledge_entity_aliases(entity_id, alias) VALUES ($1,'星澜')",
            entity_id,
        )
        kb_id = await connection.fetchval(
            "INSERT INTO knowledge_bases(owner_id, name, description) "
            "VALUES ($1::uuid, '竞品A', '') RETURNING id", OWNER,
        )
        collection_id = await connection.fetchval(
            "INSERT INTO knowledge_collections(knowledge_base_id, name) "
            "VALUES ($1::uuid, '财报') RETURNING id", kb_id,
        )
        await connection.executemany(
            """INSERT INTO knowledge_document_links(knowledge_base_id, collection_id, document_id)
               VALUES ($1::uuid, $2::uuid, $3::uuid)""",
            [(kb_id, collection_id, finance.id), (kb_id, None, pricing.id)],
        )
    return {
        "finance": finance.id, "finance_gen": finance_gen,
        "pricing": pricing.id, "pricing_gen": pricing_gen,
        "kb": str(kb_id), "collection": str(collection_id),
        "entity": str(entity_id),
    }


def _settings_stub():
    from open_deep_research.documents.settings import DocumentSettings

    return DocumentSettings(table_row_group_size=40)


async def test_unified_search_answer_expansion_and_evaluation(monkeypatch):
    monkeypatch.setenv("DOCUMENT_RESEARCH_ENABLED", "true")
    monkeypatch.setenv(
        "DOCUMENT_DATABASE_URL",
        _TEST_DSN.replace("postgresql+asyncpg://", "postgresql://", 1),
    )
    monkeypatch.setattr(search_service, "embed_texts", _fake_embed)
    monkeypatch.setattr(search_service, "knowledge_service_key", lambda: "sk-test")
    async def _usage_noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(answer, "check_and_count_usage", _usage_noop)
    from open_deep_research.documents.database import close_document_pool

    pool = await get_document_pool()
    seeded = await _seed(pool)
    try:
        # --- unified search: filter + quota + ledger ---
        async def fake_rerank(question, candidates):
            if "太空电梯" in question:  # 无关问题：全部判为无关，检索应无结果
                return {str(row["id"]): (0, "无关") for row in candidates}
            return {str(row["id"]): (3, "直接支持") for row in candidates}

        monkeypatch.setattr(search_service, "rerank_segments", fake_rerank)
        unscoped = await search_service.resolve_scope(
            search_service.SearchRequest(owner_id=OWNER, query="营收")
        )
        assert seeded["finance"] in {
            item["document_id"] for item in unscoped["documents"]
        }
        foreign = await search_service.resolve_scope(
            search_service.SearchRequest(owner_id=str(uuid.uuid4()), query="营收")
        )
        assert foreign["documents"] == []
        result = await search_service.unified_search(
            search_service.SearchRequest(
                owner_id=OWNER,
                query="星澜 2025 财年营收",
                kb_ids=[seeded["kb"]],
                filters={"doc_types": ["财报"]},
                debug=True,
            )
        )
        assert result["rerank_completed"] is True
        assert result["results"] and all(
            item["document_id"] == seeded["finance"] for item in result["results"]
        )
        assert result["diagnostics"]["resolved_scope"]["documents"] == 2
        assert result["diagnostics"]["alias_expansions"] == ["星澜"]
        async with pool.acquire() as connection:
            ledger = await connection.fetchrow(
                "SELECT * FROM knowledge_queries WHERE id=$1::uuid", result["query_id"]
            )
        assert ledger is not None and ledger["feedback_kind"] is None

        # --- rerank failure degrades honestly ---
        async def broken_rerank(question, candidates):
            raise RerankUnavailableError("rerank_upstream_unavailable:Test")

        monkeypatch.setattr(search_service, "rerank_segments", broken_rerank)
        degraded = await search_service.unified_search(
            search_service.SearchRequest(owner_id=OWNER, query="HX-200 单价",
                                         document_ids=[seeded["pricing"]])
        )
        assert degraded["rerank_completed"] is False and degraded["results"]
        monkeypatch.setattr(search_service, "rerank_segments", fake_rerank)

        # --- answers: no evidence short-circuits the model ---
        calls = {"model": 0}

        async def fake_model(question, evidence):
            calls["model"] += 1
            top = evidence[0]["segment_id"]
            if calls["model"] == 1:
                return {
                    "answer": "营收 48.6 亿元[9]",
                    "support": "sufficient",
                    "citations": [{"marker": "[9]", "segment_ids": ["ghost"]}],
                }
            return {
                "answer": "星澜科技 2025 财年营收 48.6 亿元[1]。",
                "support": "sufficient",
                "citations": [{"marker": "[1]", "segment_ids": [top]}],
            }

        monkeypatch.setattr(answer, "_call_answer_model", fake_model)
        answered = await answer.answer_question(
            search_service.SearchRequest(
                owner_id=OWNER, query="星澜 2025 财年营收",
                document_ids=[seeded["finance"]],
            )
        )
        assert answered["status"] == "answered"
        assert calls["model"] == 2  # one bad-citation round, one repair
        assert answered["citations"][0]["segment_ids"][0] in {
            item["segment_id"] for item in answered["evidence"]
        }
        empty = await answer.answer_question(
            search_service.SearchRequest(
                owner_id=OWNER, query="2030 年太空电梯规划",
                document_ids=[seeded["finance"]],
            )
        )
        assert empty["status"] == "no_evidence" and empty["message"] == "未找到支持材料"
        assert calls["model"] == 2  # no-evidence path never calls the model

        # --- KB-03: source_selection with a knowledge base expands ---
        selection = SourceSelection.model_validate(
            {"mode": "documents",
             "sources": [{"type": "knowledge_base", "id": seeded["kb"]}]}
        )
        rows = await repository.validate_selection(OWNER, selection)
        assert {str(row["id"]) for row in rows} == {seeded["finance"], seeded["pricing"]}
        await repository.bind_run_sources("run-kb", OWNER, rows)
        assert sorted(await repository.run_source_document_ids("run-kb")) == sorted(
            [seeded["finance"], seeded["pricing"]]
        )

        # --- evaluation harness over the live pipeline ---
        created = await evaluation.create_eval_set(
            "live-smoke",
            [
                {"id": "q1", "category": "zh_facts_names",
                 "question": "星澜 2025 财年营收",
                 "document_ids": [seeded["finance"]],
                 "expected": {"answerable": True, "snippets": ["48.6 亿元"]}},
                {"id": "q2", "category": "no_answer",
                 "question": "2030 年太空电梯规划",
                 "document_ids": [seeded["finance"]],
                 "expected": {"answerable": False}},
            ],
        )
        run = await evaluation.run_evaluation(
            OWNER, await evaluation.get_eval_set(created["id"]), with_answers=True
        )
        metrics = run["metrics"]
        assert metrics["recall_at_12"] == 1.0 and metrics["ndcg_at_12"] == 1.0
        assert metrics["no_answer_error_rate"] == 0.0
        assert metrics["citation_validity"] == 1.0
        assert metrics["cost"] == "unknown"

        # --- feedback lands on the pending list ---
        from open_deep_research.documents.retrieval import locator_dict

        async with pool.acquire() as connection:
            await connection.execute(
                """UPDATE knowledge_queries SET feedback_kind='not_found', feedback_note='缺年份'
                    WHERE owner_id=$1::uuid AND feedback_kind IS NULL""",
                OWNER,
            )
            rows_feedback = await connection.fetch(
                "SELECT feedback_kind FROM knowledge_queries WHERE owner_id=$1::uuid "
                "AND feedback_kind IS NOT NULL", OWNER,
            )
        assert rows_feedback and all(row["feedback_kind"] for row in rows_feedback)
        _ = locator_dict  # imported for parity with other live tests
        # A057: the public contract supplies an ISO string, not a datetime object.
        from datetime import datetime, timedelta, timezone

        for cutoff, expected_count in (
            ("2000-01-01", 0),
            ((datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat(), 2),
        ):
            historical = await search_service.resolve_scope(
                search_service.SearchRequest(
                    owner_id=OWNER, query="营收", kb_ids=[seeded["kb"]],
                    version_mode="as_of", as_of_published=cutoff,
                )
            )
            assert len(historical["documents"]) == expected_count
    finally:
        async with pool.acquire() as connection:
            await connection.execute(
                "DELETE FROM knowledge_eval_runs WHERE owner_id=$1::uuid", OWNER)
            await connection.execute(
                "DELETE FROM knowledge_eval_sets WHERE name='live-smoke'")
            await connection.execute(
                "DELETE FROM knowledge_queries WHERE owner_id=$1::uuid", OWNER)
            await connection.execute(
                "DELETE FROM knowledge_saved_searches WHERE owner_id=$1::uuid", OWNER)
            await connection.execute(
                "DELETE FROM research_run_sources WHERE owner_id=$1::uuid", OWNER)
            # 文档经 home_knowledge_base_id 外键引用知识库，先删文档。
            await connection.execute(
                "DELETE FROM research_documents WHERE owner_id=$1::uuid", OWNER)
            await connection.execute(
                "DELETE FROM knowledge_entities WHERE owner_id=$1::uuid", OWNER)
            await connection.execute(
                "DELETE FROM knowledge_jobs WHERE knowledge_base_id IN (SELECT id FROM knowledge_bases WHERE owner_id=$1::uuid)", OWNER)
            await connection.execute(
                "DELETE FROM knowledge_bases WHERE owner_id=$1::uuid", OWNER)
        await close_document_pool()
