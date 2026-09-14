"""Deterministic metadata candidates with evidence and alias matching."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from open_deep_research.documents import metadata_suggest as suggest
from open_deep_research.documents.settings import DocumentSettings
from open_deep_research.documents.structuring import StructuredUnit

OWNER = "11111111-1111-1111-1111-111111111111"
ENTITY = "22222222-2222-2222-2222-222222222222"
ENTITY_OTHER = "33333333-3333-3333-3333-333333333333"


def _unit(text: str, unit_type: str = "paragraph", **attributes) -> StructuredUnit:
    return StructuredUnit(
        unit_type=unit_type,
        locator={"source": f"{unit_type}:1"},
        raw_text=text,
        index_text=text,
        attributes=attributes or {"parse_method": "test"},
    )


def test_doc_type_and_period_and_date_come_with_evidence():
    units = [
        _unit("星澜科技 2025 年度财务报表", "title"),
        _unit("发布日期：2026-03-31"),
        _unit("本报告涵盖 2025 财年经营情况，含资产负债表与利润表。"),
    ]
    candidates = suggest.deterministic_candidates(units, "annual.pdf")
    assert candidates["doc_type"]["value"] == "财报"
    assert candidates["doc_type"]["evidence"][0]["excerpt"]
    assert candidates["period"]["value"]["year"] == "2025"
    assert candidates["publish_date"]["value"] == "2026-03-31"
    assert candidates["language"]["value"] == "zh"


def test_validity_range_candidate_carries_both_bounds():
    units = [_unit("本价格表有效期 2025-06-01 至 2026-05-31")]
    candidates = suggest.deterministic_candidates(units, "price.pdf")
    assert candidates["validity"]["value"] == {"start": "2025-06-01", "end": "2026-05-31"}


def test_fields_without_evidence_stay_absent():
    candidates = suggest.deterministic_candidates([_unit("普通内容若干。")], "note.txt")
    assert "doc_type" not in candidates
    assert "period" not in candidates
    assert "publish_date" not in candidates
    assert "validity" not in candidates


@pytest.mark.asyncio
async def test_company_matches_confirmed_aliases_and_flags_ambiguity(monkeypatch):
    class Connection:
        async def fetch(self, sql, *args):
            assert OWNER in str(args)
            return [
                {"alias": "星澜科技", "entity_id": ENTITY, "entity_kind": "company",
                 "name": "星澜科技股份有限公司"},
                {"alias": "星澜科技", "entity_id": ENTITY_OTHER, "entity_kind": "company",
                 "name": "星澜科技（旧名）"},
            ]

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    async def pool():
        return Pool()

    monkeypatch.setattr(suggest, "get_document_pool", pool)
    company, flags = await suggest.suggest_company_candidates(
        OWNER, [_unit("星澜科技 2025 财年营收")], "report.pdf"
    )
    assert company is not None and company["company"][0]["ambiguous"] is True
    assert flags == ["company_alias_ambiguous:星澜科技"]


@pytest.mark.asyncio
async def test_build_suggestions_flags_model_skip_without_model(monkeypatch):
    async def no_aliases(owner_id):
        return {}

    monkeypatch.setattr(suggest, "_owner_aliases", no_aliases)
    result = await suggest.build_suggestions(
        OWNER,
        [_unit("2025 财年价格表，报价如下")],
        "price.pdf",
        DocumentSettings(),
    )
    assert result["candidates"]["doc_type"]["value"] == "价格表"
    assert "metadata_model_skipped" in result["flags"]
