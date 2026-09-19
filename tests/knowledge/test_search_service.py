"""Unified search pieces: alias expansion, filters, citations, metrics."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from open_deep_research.knowledge import answer, rerank, search_service
from open_deep_research.knowledge.credentials import (
    KnowledgeCredentialError,
    knowledge_service_key,
)
from open_deep_research.knowledge.evaluation import _snippet_hit, ndcg_at_k, recall_at_k


@pytest.mark.asyncio
@pytest.mark.parametrize("cutoff", [None, "2026-02-30", "not-a-date", "20260919"])
async def test_as_of_rejects_invalid_dates_before_database_access(monkeypatch, cutoff):
    async def unexpected_database():
        raise AssertionError("invalid date reached database")

    monkeypatch.setattr(search_service, "get_document_pool", unexpected_database)
    with pytest.raises(search_service.SearchScopeError, match="as_of_.*date"):
        await search_service.resolve_scope(search_service.SearchRequest(
            owner_id="owner", query="q", version_mode="as_of", as_of_published=cutoff,
        ))


def test_recall_and_ndcg_at_k():
    results = [{"text": "营收 48.6 亿"}, {"text": "其他"}, {"text": "毛利率 31.2%"}]
    expected = ["营收 48.6 亿", "毛利率 31.2%"]
    assert recall_at_k(expected, results, 12) == 1.0
    assert recall_at_k(["不存在"], results, 12) == 0.0
    # Both hits at ranks 1 and 3: DCG = 1/log2(3) + 1/log2(5); ideal ranks 1,2.
    # Zero-based ranks: hits at 1 and 3 give 1/log2(2)=1, 1/log2(4)=0.5;
    # the ideal (ranks 1,2) is 1 + 1/log2(3).
    ndcg = ndcg_at_k(expected, results, 12)
    ideal = 1.0 + 1.0 / 1.58496
    assert ndcg == pytest.approx(1.5 / ideal, rel=1e-3)


def test_snippet_hit_checks_context_too():
    results = [{"text": "正文", "context_before": "有效期 2025-06-01 至 2026-05-31"}]
    assert _snippet_hit("有效期 2025-06-01", results)


def test_metadata_filter_sql_builds_intersections():
    clause, args = search_service._metadata_filter_sql(
        {"doc_types": ["财报", "价格表"], "languages": ["zh"],
         "entity_ids": ["11111111-1111-1111-1111-111111111111"],
         "publish_date_start": "2025-01-01"}
    )
    assert "doc_type" in clause and "ANY($1::text[])" in clause
    assert "language" in clause and "entity_id" in clause
    assert ">= $4" in clause
    assert args[0] == ["财报", "价格表"] and args[2] == ["11111111-1111-1111-1111-111111111111"]


@pytest.mark.asyncio
async def test_alias_expansion_skips_ambiguous_and_known(monkeypatch):
    class Connection:
        async def fetch(self, sql, *args):
            return [
                {"alias": "星澜", "entity_id": "1" * 32, "name": "星澜科技"},
                {"alias": "星澜", "entity_id": "2" * 32, "name": "星澜传媒"},  # ambiguous
                {"alias": "HLNE", "entity_id": "3" * 32, "name": "恒岳新能源"},
            ]

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    async def pool():
        return Pool()

    monkeypatch.setattr(search_service, "get_document_pool", pool)
    expanded, hits = await search_service.expand_entity_aliases(
        "11111111-1111-1111-1111-111111111111",
        "HLNE 2025 财年营收 星澜",
    )
    # HLNE 无歧义 → 展开为 canonical 名；星澜 有歧义 → 不展开、不进扩展列表
    assert "恒岳新能源" in expanded and "星澜科技" not in expanded
    assert hits == ["HLNE"]


def test_parse_rerank_response_drops_unknown_ids_and_bad_grades():
    candidates = [{"id": "a"}, {"id": "b"}]
    mapped = rerank.parse_rerank_response(
        '{"scores": [{"id": "a", "score": 3, "reason": "直接支持"},'
        ' {"id": "ghost", "score": 3}, {"id": "b", "score": 9}]}',
        candidates,
    )
    assert mapped == {"a": (3, "直接支持")}
    with pytest.raises(rerank.RerankUnavailableError):
        rerank.parse_rerank_response('{"scores": []}', candidates)
    with pytest.raises(rerank.RerankUnavailableError):
        rerank.parse_rerank_response("not json", candidates)


def test_missing_service_key_is_a_distinct_error(monkeypatch):
    monkeypatch.delenv("LITELLM_SERVICE_KEY", raising=False)
    with pytest.raises(KnowledgeCredentialError) as exc:
        knowledge_service_key()
    assert str(exc.value) == "knowledge_service_key_unavailable"


def _evidence(*ids: str) -> list[dict]:
    return [{"segment_id": item, "text": "t"} for item in ids]


def test_validate_citations_accepts_only_known_segments():
    payload = {
        "answer": "营收为 X[1]",
        "citations": [{"marker": "[1]", "segment_ids": ["s1"]}],
    }
    valid, problems = answer.validate_citations(payload, _evidence("s1", "s2"))
    assert valid == [{"marker": "[1]", "segment_ids": ["s1"]}] and problems == []
    forged_payload = {
        "answer": "营收为 X[1]",
        "citations": [{"marker": "[1]", "segment_ids": ["s2"]}],  # 不在本请求证据集内
    }
    forged, problems = answer.validate_citations(forged_payload, _evidence("s1"))
    assert not forged and any(p.startswith("unknown_segment") for p in problems)


def test_validate_citations_rejects_uncited_or_citation_free_answers():
    payload = {"answer": "没有任何引用标记的答案", "citations": [{"marker": "[1]", "segment_ids": ["s1"]}]}
    _, problems = answer.validate_citations(payload, _evidence("s1"))
    assert "answer_without_citations" in problems
    payload2 = {"answer": "引用了 [2]", "citations": [{"marker": "[1]", "segment_ids": ["s1"]}]}
    _, problems2 = answer.validate_citations(payload2, _evidence("s1"))
    assert "uncited_marker_in_answer" in problems2


def test_evaluate_answer_payload_flags_fabricated_citations():
    from open_deep_research.knowledge.evaluation import evaluate_answer_payload

    payload = {
        "status": "answered",
        "citations": [{"marker": "[1]", "segment_ids": ["ghost"]}],
        "evidence": _evidence("s1"),
    }
    assert evaluate_answer_payload(payload)["citation_valid"] is False
    ok = {
        "status": "answered",
        "citations": [{"marker": "[1]", "segment_ids": ["s1"]}],
        "evidence": _evidence("s1"),
    }
    assert evaluate_answer_payload(ok)["citation_valid"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("repair_succeeds", [True, False])
async def test_invalid_answer_json_uses_one_repair(monkeypatch, repair_succeeds):
    evidence = _evidence("s1")

    async def search(_request):
        return {"query_id": "q1", "results": evidence, "rerank_completed": True}

    async def usage(*_args):
        return None

    calls = []

    async def model(question, _evidence):
        calls.append(question)
        if len(calls) == 1 or not repair_succeeds:
            raise answer.AnswerUnavailableError("answer_invalid_response")
        return {"answer": "营收 48.6 亿[1]", "support": "sufficient",
                "citations": [{"marker": "[1]", "segment_ids": ["s1"]}]}

    monkeypatch.setattr(answer, "unified_search", search)
    monkeypatch.setattr(answer, "check_and_count_usage", usage)
    monkeypatch.setattr(answer, "_call_answer_model", model)
    result = await answer.answer_question(
        search_service.SearchRequest(owner_id="owner", query="营收是多少？")
    )
    assert len(calls) == 2 and "invalid_json" in calls[1]
    assert result["status"] == ("answered" if repair_succeeds else "citation_error")
    assert result["evidence"] == evidence
    if not repair_succeeds:
        assert result["answer"] is None
