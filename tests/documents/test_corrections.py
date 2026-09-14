"""Workbench corrections: revision lock, exclusions, merges, diff."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from open_deep_research.documents import corrections


class CorrectionsConnection:
    """Two-phase fake: planning reads, then the locked transaction."""

    def __init__(self):
        self.statements: list[str] = []
        self.plan_state: dict[str, Any] | None = None  # unlocked generation read
        self.plan_units: list[dict] = []  # units read during planning
        self.locked_state: dict[str, Any] | None = None
        self.executed: list[str] = []

    async def fetchrow(self, sql, *args):
        self.statements.append(sql)
        if "FOR UPDATE OF g" in sql:
            return self.locked_state
        return self.plan_state

    async def fetch(self, sql, *args):
        self.statements.append(sql)
        if "research_document_units WHERE generation_id" in sql:
            return self.plan_units
        return []

    async def fetchval(self, sql, *args):
        self.statements.append(sql)
        if "RETURNING revision" in sql:
            return 5
        if "coalesce(max(ordinal" in sql:
            return 9
        return 1

    async def execute(self, sql, *args):
        self.statements.append(sql)
        self.executed.append(sql)

    async def executemany(self, sql, rows):
        self.statements.append(sql)

    @asynccontextmanager
    async def transaction(self):
        yield


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


def _pool(connection):
    async def get_pool():
        return _Pool(connection)

    return get_pool


def _generation_state(revision: int = 0) -> dict:
    return {
        "id": "33333333-3333-3333-3333-333333333333",
        "status": "pending_review",
        "revision": revision,
        "metadata_snapshot": "{}",
        "version_id": "44444444-4444-4444-4444-444444444444",
        "owner_id": "11111111-1111-1111-1111-111111111111",
    }


def _unit_row(unit_id: str, text: str, unit_type: str = "paragraph") -> dict:
    return {
        "id": unit_id,
        "ordinal": 1,
        "unit_type": unit_type,
        "locator": '{"source": "paragraph:1"}',
        "raw_text": text,
        "revised_text": None,
        "index_text": text,
        "attributes": "{}",
        "excluded": False,
        "exclusion_reason": None,
    }


async def _fake_embed(texts, settings, **_kwargs):
    return [[0.1] * 1536 for _ in texts]


@pytest.mark.asyncio
async def test_stale_revision_conflicts_with_current(monkeypatch):
    connection = CorrectionsConnection()
    connection.plan_state = _generation_state(revision=0)
    connection.plan_units = []
    connection.locked_state = _generation_state(revision=7)
    monkeypatch.setattr(corrections, "get_document_pool", _pool(connection))
    monkeypatch.setattr(corrections, "embed_texts", _fake_embed)
    with pytest.raises(corrections.CorrectionRevisionError) as exc:
        await corrections.apply_corrections(
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
            "33333333-3333-3333-3333-333333333333",
            revision=3,
            metadata_confirmed={"doc_type": "未知"},
        )
    assert exc.value.current_revision == 7
    assert not any("UPDATE research_document_generations" in sql for sql in connection.executed)


@pytest.mark.asyncio
async def test_metadata_confirmation_bumps_revision_and_audits(monkeypatch):
    connection = CorrectionsConnection()
    connection.plan_state = _generation_state(revision=0)
    connection.plan_units = []
    connection.locked_state = _generation_state(revision=0)
    monkeypatch.setattr(corrections, "get_document_pool", _pool(connection))
    monkeypatch.setattr(corrections, "embed_texts", _fake_embed)
    result = await corrections.apply_corrections(
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
        "33333333-3333-3333-3333-333333333333",
        revision=0,
        metadata_confirmed={"doc_type": "财报", "period": "未知"},
    )
    assert result == {
        "revision": 5,
        "applied": {
            "units_corrected": 0, "units_excluded": 0, "units_merged": 0,
            "units_split": 0, "segments_rebuilt": 0, "metadata_fields_confirmed": 2,
        },
    }
    assert any("'correct'" in sql for sql in connection.executed)
    assert any("jsonb_set" in sql and "confirmed" in sql for sql in connection.executed)


@pytest.mark.asyncio
async def test_excluded_unit_drops_its_segments(monkeypatch):
    connection = CorrectionsConnection()
    connection.plan_state = _generation_state(revision=1)
    connection.plan_units = [_unit_row("55555555-5555-5555-5555-555555555555", "不可靠段落")]
    connection.locked_state = _generation_state(revision=1)
    monkeypatch.setattr(corrections, "get_document_pool", _pool(connection))
    monkeypatch.setattr(corrections, "embed_texts", _fake_embed)
    result = await corrections.apply_corrections(
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
        "33333333-3333-3333-3333-333333333333",
        revision=1,
        unit_corrections=[
            {"unit_id": "55555555-5555-5555-5555-555555555555",
             "excluded": True, "exclusion_reason": "OCR 乱码"},
        ],
    )
    assert result["applied"]["units_excluded"] == 1
    assert any(
        "DELETE FROM research_document_segments WHERE unit_id" in sql
        for sql in connection.executed
    )


@pytest.mark.asyncio
async def test_published_generation_is_not_editable(monkeypatch):
    connection = CorrectionsConnection()
    monkeypatch.setattr(corrections, "get_document_pool", _pool(connection))
    from open_deep_research.documents.repository import DocumentConflictError

    connection.plan_state = _generation_state() | {"status": "published"}
    connection.plan_units = []
    connection.locked_state = _generation_state() | {"status": "published"}
    with pytest.raises(DocumentConflictError, match="generation_not_editable"):
        await corrections.apply_corrections(
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
            "33333333-3333-3333-3333-333333333333",
            revision=0,
            metadata_confirmed={"doc_type": "其他"},
        )


def test_merged_table_rows_drops_repeated_header():
    left = {"raw_text": "指标 | 数值\n营收 | 100"}
    right = {"raw_text": "指标 | 数值\n毛利 | 30"}
    assert corrections._merged_table_rows(left, right) == (
        "指标 | 数值\n营收 | 100\n毛利 | 30"
    )


def test_effective_text_prefers_revision():
    unit = {"revised_text": "人工改写", "index_text": "原始", "raw_text": "原始"}
    assert corrections.effective_text(unit) == "人工改写"
    unit["revised_text"] = None
    assert corrections.effective_text(unit) == "原始"
