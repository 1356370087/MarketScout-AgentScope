"""Document persistence must accept the existing local development identity."""
import re
from contextlib import asynccontextmanager
from uuid import UUID

import pytest

from open_deep_research.documents import repository, retrieval, versioning
from open_deep_research.documents.contracts import SourceSelection
from open_deep_research.knowledge import search_service
from security.rbac.principal import synthetic_dev_principal


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [None, "b4f63627-81d8-461e-95c6-d26471c0b570"])
async def test_document_list_binds_uuid_for_dev_and_regular_owners(monkeypatch, owner):
    owner = owner or synthetic_dev_principal().user_id
    seen = []

    class Connection:
        async def fetchval(self, sql, *args):
            UUID(args[0])  # Same UUID parameter boundary as asyncpg.
            seen.append(args[0])
            return 0

        async def fetch(self, sql, *args):
            UUID(args[0])
            seen.append(args[0])
            return []

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    async def pool():
        return Pool()

    monkeypatch.setattr(repository, "get_document_pool", pool)
    assert await repository.list_documents(owner, limit=100) == ([], 0)
    assert len(set(seen)) == 1
    if owner != "local-dev-user":
        assert seen[0] == owner


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [
    "get_document", "get_chunk", "list_chunks", "retry_document",
    "queue_reindex_generation", "soft_delete_document", "validate_selection",
    "bind_run_sources", "search_document_chunks",
])
async def test_all_document_owner_boundaries_accept_dev_identity(monkeypatch, operation):
    doc_id = "b4f63627-81d8-461e-95c6-d26471c0b570"
    owner = synthetic_dev_principal().user_id
    calls = []

    class Connection:
        @asynccontextmanager
        async def transaction(self):
            yield

        async def fetch(self, sql, *args):
            for index in re.findall(r"\$(\d+)::uuid(?!\[)", sql):
                value = args[int(index) - 1]
                if value is not None:  # generation snapshots may be absent
                    UUID(value)
            calls.append(args)
            return []

        async def fetchrow(self, sql, *args):
            await self.fetch(sql, *args)
            return None

        async def fetchval(self, sql, *args):
            await self.fetch(sql, *args)
            return False

        async def executemany(self, sql, rows):
            for row in rows:
                await self.fetch(sql, *row)

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    async def pool():
        return Pool()

    async def embed(*args, **kwargs):
        return [[0.1, 0.2]]

    monkeypatch.setattr(repository, "get_document_pool", pool)
    monkeypatch.setattr(retrieval, "get_document_pool", pool)
    monkeypatch.setattr(search_service, "get_document_pool", pool)
    if operation == "bind_run_sources":
        await repository.bind_run_sources("test-run", owner, [
            {"id": doc_id, "filename": "test.txt", "sha256": "a" * 64,
             "current_generation_id": None},
        ])
    elif operation == "validate_selection":
        selection = SourceSelection(mode="documents", sources=[{"type": "document", "id": doc_id}])
        with pytest.raises(KeyError, match="document_not_found"):
            await repository.validate_selection(owner, selection)
    elif operation == "search_document_chunks":
        assert await search_service.resolve_scope(search_service.SearchRequest(
            owner_id=owner, document_ids=[doc_id], query="test")) == {"documents": []}
    elif operation == "get_chunk":
        assert await repository.get_chunk(owner, doc_id, doc_id) is None
    else:
        module = versioning if operation == "queue_reindex_generation" else repository
        monkeypatch.setattr(module, "get_document_pool", pool)
        assert await getattr(module, operation)(owner, doc_id) is None
    assert calls
