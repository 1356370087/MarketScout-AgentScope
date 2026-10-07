"""Route semantics for /knowledge-bases (auth bypass, faked repository)."""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from fastapi.testclient import TestClient

from open_deep_research import server
from open_deep_research.knowledge.contracts import KnowledgeBaseView
from open_deep_research.knowledge.repository import KnowledgeConflictError

# The package re-exports the APIRouter as ``knowledge.router``, shadowing the
# submodule attribute; fetch the real module object for monkeypatching.
knowledge_router = importlib.import_module("open_deep_research.knowledge.router")


def _view(**overrides: Any) -> KnowledgeBaseView:
    payload = {
        "id": "11111111-1111-1111-1111-111111111111",
        "name": "竞品A",
        "description": "",
        "created_at": "2026-09-08T00:00:00+00:00",
        "updated_at": "2026-09-08T00:00:00+00:00",
    }
    payload.update(overrides)
    return KnowledgeBaseView.model_validate(payload)


@pytest.fixture
def client(monkeypatch):
    from open_deep_research.knowledge import authz

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("LOCAL_DEV_AUTH_BYPASS", "true")
    monkeypatch.delenv("IAM_DATABASE_URL", raising=False)
    monkeypatch.setattr(knowledge_router, "document_schema_available", lambda: True)
    async def _caps(_user_id, _kb_id):
        return _ALL_CAPS

    async def _readable(_user_id):
        return ["11111111-1111-1111-1111-111111111111"]

    async def _require(_user_id, _kb_id, _capability):
        return {"knowledge_base": {"visibility": "team"}}

    monkeypatch.setattr(authz, "kb_capabilities", _caps)
    monkeypatch.setattr(authz, "readable_kb_ids", _readable)
    monkeypatch.setattr(authz, "require_kb_capability", _require)
    monkeypatch.setattr(authz, "record_audit", _audit_noop)
    return TestClient(server.app, raise_server_exceptions=False)


_ALL_CAPS = frozenset(
    {"view_published", "download", "submit", "review_publish", "manage"}
)


async def _audit_noop(**_kwargs):
    return None


def test_create_conflict_maps_to_409(client, monkeypatch):
    async def conflict(*args, **kwargs):
        raise KnowledgeConflictError("knowledge_base_name_conflict")

    monkeypatch.setattr(knowledge_router, "create_knowledge_base", conflict)
    response = client.post("/knowledge-bases", json={"name": "竞品A"})
    assert response.status_code == 409
    assert response.json()["detail"] == "knowledge_base_name_conflict"


def test_detail_unknown_base_maps_to_404(client, monkeypatch):
    async def missing(*args, **kwargs):
        return None

    monkeypatch.setattr(knowledge_router, "get_knowledge_base", missing)
    assert client.get("/knowledge-bases/11111111-1111-1111-1111-111111111111").status_code == 404


def test_update_requires_a_field(client):
    response = client.patch(
        "/knowledge-bases/11111111-1111-1111-1111-111111111111", json={}
    )
    assert response.status_code == 422


def test_archive_and_restore_round_trip(client, monkeypatch):
    calls = []

    async def archive(owner, kb_id, *, archived):
        calls.append(archived)
        return _view(archived=archived)

    monkeypatch.setattr(knowledge_router, "set_knowledge_base_archived", archive)
    kb = "11111111-1111-1111-1111-111111111111"
    assert client.post(f"/knowledge-bases/{kb}/archive").json()["archived"] is True
    assert client.post(f"/knowledge-bases/{kb}/restore").json()["archived"] is False
    assert calls == [True, False]


def test_link_unknown_document_is_uniformly_not_found(client, monkeypatch):
    async def reject(*args, **kwargs):
        raise KeyError("document_not_found:33333333-3333-3333-3333-333333333333")

    monkeypatch.setattr(knowledge_router, "link_documents", reject)
    response = client.post(
        "/knowledge-bases/11111111-1111-1111-1111-111111111111/documents",
        json={"document_ids": ["33333333-3333-3333-3333-333333333333"]},
    )
    assert response.status_code == 404
    assert "33333333" in response.json()["detail"]


def test_disabled_schema_gates_routes_with_503(client, monkeypatch):
    monkeypatch.setattr(knowledge_router, "document_schema_available", lambda: False)
    assert client.get("/knowledge-bases").status_code == 503
