"""Generation lifecycle state machine: publish, reject, withdraw, complete."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from open_deep_research.documents import versioning
from open_deep_research.documents.repository import DocumentConflictError

OWNER = "11111111-1111-1111-1111-111111111111"
DOC = "22222222-2222-2222-2222-222222222222"
GEN = "33333333-3333-3333-3333-333333333333"
VERSION = "44444444-4444-4444-4444-444444444444"


class ScriptedConnection:
    """Pops scripted fetchrow outcomes while recording every statement."""

    def __init__(self, *, fetchrow=(), fetchval=()):
        self.fetchrow_results = list(fetchrow)
        self.fetchval_results = list(fetchval)
        self.statements: list[str] = []
        self.execute_args: list[tuple] = []
        self.executemany_batches: list[tuple[str, list[tuple]]] = []

    async def fetchrow(self, sql, *args):
        self.statements.append(sql)
        if not self.fetchrow_results:
            return None
        outcome = self.fetchrow_results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def fetchval(self, sql, *args):
        self.statements.append(sql)
        return self.fetchval_results.pop(0) if self.fetchval_results else None

    async def execute(self, sql, *args):
        self.statements.append(sql)
        self.execute_args.append(args)

    async def executemany(self, sql, rows):
        self.statements.append(sql)
        self.executemany_batches.append((sql, list(rows)))

    @asynccontextmanager
    async def transaction(self):
        yield


def _pool(connection):
    async def get_pool():
        return _Pool(connection)

    return get_pool


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


def _state_row(status: str, current: str | None) -> dict:
    return {
        "generation_id": GEN,
        "status": status,
        "version_id": VERSION,
        "metadata_snapshot": '{"confirmed": {"doc_type": "价格表"}}',
        "current_generation_id": current,
        "owner_id": OWNER,
        "revision": 3,
    }


def _view_row() -> dict:
    return {
        "id": GEN,
        "document_id": DOC,
        "version_id": VERSION,
        "status": "published",
        "revision": 3,
        "created_at": None,
        "updated_at": None,
        "published_at": None,
        "review_note": None,
        "is_current": True,
    }


@pytest.mark.asyncio
async def test_publish_requires_confirmed_metadata(monkeypatch):
    connection = ScriptedConnection(
        fetchrow=[_state_row("pending_review", None) | {"metadata_snapshot": "{}"}]
    )
    monkeypatch.setattr(versioning, "get_document_pool", _pool(connection))
    with pytest.raises(DocumentConflictError) as exc:
        await versioning.publish_generation(OWNER, DOC, GEN)
    assert str(exc.value) == "metadata_not_confirmed"
    assert not any("current_generation_id=$2" in sql for sql in connection.statements)


@pytest.mark.asyncio
async def test_publish_flips_status_pointer_and_records_operation(monkeypatch):
    connection = ScriptedConnection(fetchrow=[_state_row("pending_review", None), _view_row()])
    monkeypatch.setattr(versioning, "get_document_pool", _pool(connection))
    view = await versioning.publish_generation(OWNER, DOC, GEN)
    assert view["status"] == "published" and view["is_current"]
    assert any("SET status='published'" in sql for sql in connection.statements)
    assert any("current_generation_id=$2::uuid" in sql for sql in connection.statements)
    assert any("INSERT INTO research_document_operations" in sql for sql in connection.statements)


@pytest.mark.asyncio
async def test_publishing_current_generation_is_an_idempotent_noop(monkeypatch):
    connection = ScriptedConnection(fetchrow=[_state_row("published", GEN), _view_row()])
    monkeypatch.setattr(versioning, "get_document_pool", _pool(connection))
    view = await versioning.publish_generation(OWNER, DOC, GEN)
    assert view["status"] == "published"
    assert not any("INSERT INTO research_document_operations" in sql for sql in connection.statements)
    assert not any("SET status='published'" in sql for sql in connection.statements)


@pytest.mark.asyncio
async def test_publish_rejected_generation_conflicts(monkeypatch):
    connection = ScriptedConnection(fetchrow=[_state_row("rejected", None)])
    monkeypatch.setattr(versioning, "get_document_pool", _pool(connection))
    with pytest.raises(DocumentConflictError) as exc:
        await versioning.publish_generation(OWNER, DOC, GEN)
    assert str(exc.value) == "generation_not_publishable"


@pytest.mark.asyncio
async def test_publishing_old_published_generation_only_moves_pointer(monkeypatch):
    other = "55555555-5555-5555-5555-555555555555"
    connection = ScriptedConnection(fetchrow=[_state_row("published", other), _view_row()])
    monkeypatch.setattr(versioning, "get_document_pool", _pool(connection))
    await versioning.publish_generation(OWNER, DOC, GEN)
    assert not any("SET status='published'" in sql for sql in connection.statements)
    assert any("current_generation_id=$2::uuid" in sql for sql in connection.statements)
    assert "set_current" in [args[3] for args in connection.execute_args if len(args) >= 4]


@pytest.mark.asyncio
async def test_reject_refuses_published_generation(monkeypatch):
    connection = ScriptedConnection(fetchrow=[_state_row("published", GEN)])
    monkeypatch.setattr(versioning, "get_document_pool", _pool(connection))
    with pytest.raises(DocumentConflictError) as exc:
        await versioning.reject_generation(OWNER, DOC, GEN, reason="误传")
    assert str(exc.value) == "generation_not_rejectable"


@pytest.mark.asyncio
async def test_withdraw_clears_pointer_only_for_current_generation(monkeypatch):
    connection = ScriptedConnection(
        fetchrow=[{"generation_id": GEN, "current_generation_id": GEN}]
    )
    monkeypatch.setattr(versioning, "get_document_pool", _pool(connection))
    result = await versioning.withdraw_version(OWNER, DOC, VERSION, reason="过期")
    assert result["status"] == "withdrawn"
    assert result["cleared_current_pointer"] is True
    assert any("SET status='withdrawn'" in sql for sql in connection.statements)
    assert any("SET current_generation_id=NULL" in sql for sql in connection.statements)

    other = "55555555-5555-5555-5555-555555555555"
    connection = ScriptedConnection(
        fetchrow=[{"generation_id": GEN, "current_generation_id": other}]
    )
    monkeypatch.setattr(versioning, "get_document_pool", _pool(connection))
    result = await versioning.withdraw_version(OWNER, DOC, VERSION)
    assert result["cleared_current_pointer"] is False
    assert not any("current_generation_id=NULL" in sql for sql in connection.statements)
