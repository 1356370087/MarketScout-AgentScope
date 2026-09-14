"""Owner-scoping and conflict mapping for the knowledge-base repository."""

from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import UUID

import asyncpg
import pytest

from open_deep_research.knowledge import repository
from security.rbac.principal import synthetic_dev_principal

OWNER = "b4f63627-81d8-461e-95c6-d26471c0b570"
KB_ID = "11111111-1111-1111-1111-111111111111"
DOC_ID = "22222222-2222-2222-2222-222222222222"


class ScriptedConnection:
    """Minimal asyncpg stand-in recording statements and popping outcomes."""

    def __init__(self, *, fetchval=(), fetch=(), fetchrow=()):
        self.fetchval_results = list(fetchval)
        self.fetch_results = list(fetch)
        self.fetchrow_results = list(fetchrow)
        self.statements: list[str] = []
        self.arguments: list[tuple] = []

    async def fetchval(self, sql, *args):
        self._record(sql, args)
        return self.fetchval_results.pop(0) if self.fetchval_results else 0

    async def fetch(self, sql, *args):
        self._record(sql, args)
        return self.fetch_results.pop(0) if self.fetch_results else []

    async def fetchrow(self, sql, *args):
        self._record(sql, args)
        if not self.fetchrow_results:
            return None
        outcome = self.fetchrow_results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def execute(self, sql, *args):
        self._record(sql, args)

    @asynccontextmanager
    async def transaction(self):
        yield

    def _record(self, sql, args):
        self.statements.append(sql)
        self.arguments.append(args)


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


@pytest.mark.asyncio
async def test_create_knowledge_base_binds_owner_uuid(monkeypatch):
    """The synthetic dev identity is translated before hitting SQL."""
    connection = ScriptedConnection(
        fetchrow=[{"id": UUID(int=1), "name": "竞品A", "description": "",
                   "archived_at": None, "created_at": None, "updated_at": None}]
    )
    monkeypatch.setattr(repository, "get_document_pool", _pool(connection))
    view = await repository.create_knowledge_base(
        synthetic_dev_principal().user_id, "竞品A", ""
    )
    assert view.id == "00000000-0000-0000-0000-000000000001"
    assert view.archived is False
    assert connection.arguments[0][0] != "local-dev-user"
    UUID(connection.arguments[0][0])  # same UUID boundary as asyncpg


@pytest.mark.asyncio
async def test_create_knowledge_base_maps_unique_violation(monkeypatch):
    connection = ScriptedConnection(
        fetchrow=[asyncpg.UniqueViolationError("duplicate key")]
    )
    monkeypatch.setattr(repository, "get_document_pool", _pool(connection))
    with pytest.raises(repository.KnowledgeConflictError) as exc:
        await repository.create_knowledge_base(OWNER, "重复名", "")
    assert str(exc.value) == "knowledge_base_name_conflict"


@pytest.mark.asyncio
async def test_link_documents_reports_missing_ids_without_insert(monkeypatch):
    """Cross-owner or unknown documents abort the link before any INSERT."""
    connection = ScriptedConnection(fetchval=[True], fetch=[[{"id": UUID(DOC_ID)}]])
    monkeypatch.setattr(repository, "get_document_pool", _pool(connection))
    with pytest.raises(KeyError) as exc:
        await repository.link_documents(
            OWNER, KB_ID,
            [DOC_ID, "33333333-3333-3333-3333-333333333333"],
        )
    message = exc.value.args[0]
    assert message.startswith("document_not_found:")
    assert "33333333-3333-3333-3333-333333333333" in message
    assert not any(
        "INSERT INTO knowledge_document_links" in statement
        for statement in connection.statements
    )


@pytest.mark.asyncio
async def test_link_documents_rejects_foreign_collection(monkeypatch):
    connection = ScriptedConnection(fetchval=[True, False])
    monkeypatch.setattr(repository, "get_document_pool", _pool(connection))
    with pytest.raises(KeyError) as exc:
        await repository.link_documents(
            OWNER, KB_ID, [DOC_ID],
            collection_id="44444444-4444-4444-4444-444444444444",
        )
    assert exc.value.args[0] == "collection_not_found"


@pytest.mark.asyncio
async def test_link_documents_returns_none_for_foreign_base(monkeypatch):
    connection = ScriptedConnection(fetchval=[False])
    monkeypatch.setattr(repository, "get_document_pool", _pool(connection))
    result = await repository.link_documents(OWNER, KB_ID, [DOC_ID])
    assert result is None
    assert len(connection.statements) == 1  # ownership probe only


@pytest.mark.asyncio
async def test_unlink_distinguishes_empty_from_foreign_base(monkeypatch):
    """No removed links can mean an empty base or somebody else's base."""
    connection = ScriptedConnection(fetch=[[]], fetchval=[False])
    monkeypatch.setattr(repository, "get_document_pool", _pool(connection))
    removed = await repository.unlink_document(OWNER, KB_ID, DOC_ID)
    assert removed is None


@pytest.mark.asyncio
async def test_unlink_returns_removed_count(monkeypatch):
    connection = ScriptedConnection(
        fetch=[[{"id": UUID(int=1)}], ],
        fetchval=[True],
    )
    monkeypatch.setattr(repository, "get_document_pool", _pool(connection))
    removed = await repository.unlink_document(OWNER, KB_ID, DOC_ID)
    assert removed == 1


@pytest.mark.asyncio
async def test_list_knowledge_documents_requires_owned_base(monkeypatch):
    connection = ScriptedConnection(fetchval=[False])
    monkeypatch.setattr(repository, "get_document_pool", _pool(connection))
    assert await repository.list_knowledge_documents(OWNER, KB_ID) is None
    assert len(connection.statements) == 1
