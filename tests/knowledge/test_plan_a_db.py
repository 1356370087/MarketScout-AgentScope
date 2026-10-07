"""Real SQL acceptance of shared retrieval, frozen evidence and service budgets."""

import asyncio
import hashlib
import json
import os
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio

from open_deep_research.documents import corrections, repository, versioning
from open_deep_research.documents.contracts import SourceSelection
from open_deep_research.documents.database import close_document_pool, get_document_pool
from open_deep_research.documents.settings import DocumentSettings
from open_deep_research.documents.storage import StagedUpload
from open_deep_research.documents.structuring import PreparedDocument, StructuredUnit
from open_deep_research.knowledge import accounting, search_service
from open_deep_research.knowledge.credentials import KnowledgeBudgetExceeded
from open_deep_research.knowledge.execution import SearchExecution
from open_deep_research.knowledge.run_scope import prepare_knowledge_sources

DSN = os.environ.get("IAM_TEST_DATABASE_URL", "")
pytestmark = [pytest.mark.asyncio, pytest.mark.db, pytest.mark.skipif(not DSN, reason="IAM_TEST_DATABASE_URL not configured")]


@pytest_asyncio.fixture
async def database(monkeypatch):
    monkeypatch.setenv("DOCUMENT_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("DOCUMENT_DATABASE_URL", DSN.replace("postgresql+asyncpg://", "postgresql://"))
    monkeypatch.setenv("DOCUMENT_EMBEDDING_MODEL", "if-embedding-v1")
    monkeypatch.setenv("DOCUMENT_EMBEDDING_REVISION", "v1")
    monkeypatch.setenv("LITELLM_SERVICE_KEY", "fixture-service-key")
    await close_document_pool()
    pool = await get_document_pool()
    try:
        yield pool
    finally:
        await close_document_pool()


@pytest_asyncio.fixture
async def corpus(monkeypatch, tmp_path, database):
    pool = database
    owner, member, outsider = (str(uuid.uuid4()) for _ in range(3))
    async with pool.acquire() as c:
        workspace = await c.fetchval("INSERT INTO knowledge_workspaces(kind,name,created_by) VALUES('team','Plan A fixture',$1::uuid) RETURNING id", owner)
        await c.executemany("INSERT INTO knowledge_workspace_members(workspace_id,user_id,role) VALUES($1,$2::uuid,$3)", [(workspace, owner, "owner"), (workspace, member, "member")])
        base = await c.fetchval("INSERT INTO knowledge_bases(owner_id,name,description,workspace_id,visibility,created_by) VALUES($1::uuid,'Plan A','',$2,'team',$1::uuid) RETURNING id", owner, workspace)
    source = tmp_path / "pricing.md"
    source.write_text("Acme pricing evidence", encoding="utf-8")
    body = source.read_bytes()
    doc, _ = await repository.create_document(owner, StagedUpload(source, "pricing.md", "text/markdown", len(body), hashlib.sha256(body).hexdigest()), "fixture/pricing.md", DocumentSettings(), knowledge_base_id=str(base))
    draft = await versioning.latest_draft_generation(doc.id)
    generation = str(draft["id"])
    texts = [f"Acme row {index}: USD {100 + index}; annual contract." for index in range(206)]
    prepared = PreparedDocument(units=[StructuredUnit(unit_type="table", locator={"source": "sheet:Pricing", "sheet": "Pricing"},
        raw_text="Currency USD; annual contract.\n" + "\n".join(texts),
        index_text="Currency USD; annual contract.\n" + "\n".join(texts))],
        segment_texts=texts, segment_units=[0] * len(texts))
    await versioning.complete_generation_rich(generation, prepared, [[0.1] * 1536 for _ in texts], embedding_model="if-embedding-v1")
    await corrections.apply_corrections(owner, doc.id, generation, revision=0, metadata_confirmed={"doc_type": "价格表"})
    await versioning.publish_generation(owner, doc.id, generation)
    async with pool.acquire() as c:
        await c.execute("UPDATE research_document_jobs SET status='completed' WHERE document_id=$1::uuid", doc.id)
        segments = await c.fetch("SELECT id,ordinal,unit_id FROM research_document_segments WHERE generation_id=$1::uuid ORDER BY ordinal", generation)

    async def embed(texts, *_args, **_kwargs):
        return [[0.1] * 1536 for _ in texts]

    async def rerank(question, candidates, **kwargs):
        return {str(item["id"]): (3 if item["ordinal"] == 1 else 2, "fixture") for item in candidates}

    monkeypatch.setattr(search_service, "embed_texts", embed)
    monkeypatch.setattr(search_service, "rerank_segments", rerank)
    data = SimpleNamespace(pool=pool, owner=owner, member=member, outsider=outsider,
        base=str(base), workspace=workspace, document=doc.id, generation=generation, segments=segments)
    try:
        yield data
    finally:
        async with pool.acquire() as c:
            await c.execute("DELETE FROM research_run_sources WHERE document_id=$1::uuid", doc.id)
            await c.execute("DELETE FROM research_documents WHERE id=$1::uuid", doc.id)
            await c.execute("DELETE FROM knowledge_jobs WHERE knowledge_base_id=$1::uuid", base)
            await c.execute("DELETE FROM knowledge_bases WHERE id=$1::uuid", base)
            await c.execute("DELETE FROM knowledge_fact_keys WHERE workspace_id=$1", workspace)
            await c.execute("DELETE FROM knowledge_workspaces WHERE id=$1", workspace)
            await c.execute("DELETE FROM knowledge_queries WHERE owner_id=ANY($1::uuid[])", [owner, member, outsider])
            await c.execute("DELETE FROM knowledge_model_attempts WHERE owner_id=ANY($1::uuid[])", [owner, member, outsider])
            await c.execute("DELETE FROM knowledge_usage_daily WHERE owner_id=ANY($1::uuid[])", [owner, member, outsider])


async def test_shared_search_sorts_honors_limit_and_reports_real_candidates(corpus):
    result = await search_service.unified_search(search_service.SearchRequest(owner_id=corpus.member,
        query="Acme", kb_ids=[corpus.base], limit=1, debug=True))
    assert len(result["results"]) == 1
    assert result["results"][0]["segment_id"] == str(corpus.segments[1]["id"])
    assert result["results"][0]["relevance"] == 3
    assert "Currency USD" in result["results"][0]["parent_context"]
    assert result["diagnostics"]["route_candidates"]["vector"] == 40
    assert result["diagnostics"]["resolved_scope"]["generations"][0]["generation_id"] == corpus.generation
    scope = SourceSelection.model_validate({"mode": "documents", "sources": [{"type": "knowledge_base", "id": corpus.base}]})
    prepared = await prepare_knowledge_sources(corpus.member, scope)
    assert prepared["knowledge_manifest"]["documents"][0]["generation_id"] == corpus.generation


async def test_parent_only_table_context_keeps_headers_notes_and_total_budget(corpus, monkeypatch):
    from unittest.mock import AsyncMock

    async with corpus.pool.acquire() as c:
        await c.execute("UPDATE research_document_units SET attributes=$2::jsonb WHERE id=$1",
            corpus.segments[0]["unit_id"], json.dumps({"header": ["Plan", "Price"], "unit_note": "Currency USD",
                "footnotes": "Annual contract; excludes tax."}))
    parameters = {**search_service.DEFAULT_PARAMETERS, "version": "parent-only", "context_neighbors": 0,
                  "parent_context": True, "total_context_char_budget": 220}
    monkeypatch.setattr(search_service, "load_profile", AsyncMock(return_value=parameters))
    result = await search_service.unified_search(search_service.SearchRequest(owner_id=corpus.member,
        query="Acme", kb_ids=[corpus.base], limit=1))
    hit = result["results"][0]
    assert "Plan | Price" in hit["parent_context"] and "excludes tax" in hit["parent_context"]
    assert hit["context_before"] == hit["context_after"] == ""
    assert sum(len(hit[key]) for key in ("text", "parent_context", "context_before", "context_after")) <= 220


async def test_revocation_stops_search_selection_and_exact_citation(corpus):
    chunk_id = str(corpus.segments[-1]["id"])
    chunk = await repository.get_chunk(corpus.member, corpus.document, chunk_id)
    assert chunk.ordinal == 205 and chunk.generation_id == corpus.generation
    async with corpus.pool.acquire() as c:
        await c.execute("DELETE FROM knowledge_workspace_members WHERE workspace_id=$1 AND user_id=$2::uuid", corpus.workspace, corpus.member)
    assert await repository.get_chunk(corpus.member, corpus.document, chunk_id) is None
    result = await search_service.unified_search(search_service.SearchRequest(owner_id=corpus.member, query="Acme", kb_ids=[corpus.base]))
    assert result["results"] == []
    with pytest.raises(repository.DocumentConflictError):
        await repository.validate_selection(corpus.member, SourceSelection.model_validate({"mode": "documents", "sources": [{"type": "knowledge_base", "id": corpus.base}]}))


async def test_frozen_generation_survives_new_publication_and_new_profile_is_rejected(corpus, monkeypatch):
    prepared = await prepare_knowledge_sources(corpus.member, SourceSelection.model_validate({"mode": "documents", "sources": [{"type": "document", "id": corpus.document}]}))
    async with corpus.pool.acquire() as c:
        version = await c.fetchval("SELECT version_id FROM research_document_generations WHERE id=$1::uuid", corpus.generation)
        newer = await c.fetchval("INSERT INTO research_document_generations(document_id,version_id,status,published_at,index_profile) VALUES($1::uuid,$2,'published',now(),$3::jsonb) RETURNING id", corpus.document, version, json.dumps(DocumentSettings().index_profile))
        await c.execute("UPDATE research_documents SET current_generation_id=$2 WHERE id=$1::uuid", corpus.document, newer)
    from open_deep_research.models.credentials_context import (
        bind_run_key,
        reset_run_key,
    )

    token = bind_run_key("fixture-run-key")
    try:
        result = await search_service.unified_search(search_service.SearchRequest(owner_id=corpus.member, query="Acme"), execution=SearchExecution(scope="run", manifest=prepared["knowledge_manifest"]))
    finally:
        reset_run_key(token)
    assert result["results"] and all(item["generation_id"] == corpus.generation for item in result["results"])
    monkeypatch.setenv("DOCUMENT_EMBEDDING_REVISION", "v2")
    with pytest.raises(search_service.SearchScopeError, match="index_profile"):
        await search_service.unified_search(search_service.SearchRequest(owner_id=corpus.member, query="Acme", generation_ids=[corpus.generation], version_mode="pinned"))


async def test_pinned_ids_cannot_widen_explicit_scope(corpus):
    with pytest.raises(search_service.SearchScopeError, match="outside_scope"):
        await search_service.resolve_scope(search_service.SearchRequest(owner_id=corpus.member, query="q",
            document_ids=[str(uuid.uuid4())], generation_ids=[corpus.generation], version_mode="pinned"))


async def test_partial_reparse_keeps_the_index_profile_and_rejects_model_changes(corpus, monkeypatch):
    from open_deep_research.documents import reparse

    draft = await reparse.queue_scoped_reparse(corpus.owner, corpus.document, corpus.generation, sheets=["Pricing"])
    async with corpus.pool.acquire() as c:
        row = dict(await c.fetchrow("SELECT * FROM research_document_generations WHERE id=$1::uuid", draft))
    assert json.loads(row["index_profile"]) == DocumentSettings().index_profile
    assert await repository.get_chunk(corpus.member, corpus.document,
        str(uuid.uuid5(uuid.NAMESPACE_URL, f"insightforge:segment:{draft}:0"))) is None
    monkeypatch.setenv("DOCUMENT_EMBEDDING_REVISION", "v2")
    with pytest.raises(repository.DocumentConflictError, match="index_profile_mismatch"):
        await reparse.execute_scoped_reparse({}, row, DocumentSettings())


async def test_service_budget_is_atomic_across_operations_and_repairs(corpus, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_MODEL_CALLS_PER_USER_DAY", "3")
    query_id = str(uuid.uuid4())
    async with accounting.knowledge_query(corpus.member, query_id):
        attempts = await asyncio.gather(*(accounting.reserve_attempt(operation, "fixture-model") for operation in ["embedding", "rerank", "answer", "repair", "answer", "repair"]), return_exceptions=True)
        completed = [item for item in attempts if isinstance(item, str)]
        assert len(completed) == 3
        assert sum(isinstance(item, KnowledgeBudgetExceeded) for item in attempts) == 3
        for item in completed:
            await accounting.settle_attempt(item, usage={"input_tokens": 7, "output_tokens": 3})
    usage = await accounting.query_usage(query_id, corpus.member)
    assert usage["attempts"] == 3 and usage["reported"]["input_tokens"] == 21
    assert (await accounting.query_usage(query_id, corpus.outsider))["attempts"] == 0


async def test_published_assets_keep_original_evidence_and_revocation(corpus):
    from open_deep_research.knowledge.research_assets import read_assets

    async with corpus.pool.acquire() as c:
        key = await c.fetchval("INSERT INTO knowledge_fact_keys(workspace_id,entity_name,metric) VALUES($1,'Acme','Price') RETURNING id", corpus.workspace)
        fact = await c.fetchval("""INSERT INTO knowledge_fact_assertions(knowledge_base_id,fact_key_id,value_text,status,verification,published_at)
            VALUES($1::uuid,$2,'USD 100','published','verified',now()) RETURNING id""", corpus.base, key)
        await c.execute("""INSERT INTO knowledge_fact_evidence(assertion_id,document_id,generation_id,unit_id,segment_id,excerpt)
            VALUES($1,$2::uuid,$3::uuid,$4,$5,'USD 100')""", fact, corpus.document, corpus.generation,
            corpus.segments[0]["unit_id"], corpus.segments[0]["id"])
        page = await c.fetchval("INSERT INTO knowledge_pages(knowledge_base_id,title) VALUES($1::uuid,'Acme pricing') RETURNING id", corpus.base)
        revision = await c.fetchval("""INSERT INTO knowledge_page_revisions(page_id,revision_number,blocks,status,published_at)
            VALUES($1,1,'[{"id":"b1","content":"Approved price: USD 100"}]','published',now()) RETURNING id""", page)
        await c.execute("UPDATE knowledge_pages SET published_revision_id=$2 WHERE id=$1", page, revision)
        await c.execute("INSERT INTO knowledge_page_citations(revision_id,block_id,citation_type,fact_assertion_id) VALUES($1,'b1','fact',$2)", revision, fact)
    prepared = await prepare_knowledge_sources(corpus.member, SourceSelection.model_validate({"mode": "documents", "sources": [{"type": "knowledge_base", "id": corpus.base}]}))
    manifest = prepared["knowledge_manifest"]
    result = await read_assets(corpus.member, manifest, "facts", "Acme")
    assert result["items"][0]["fact_assertion_id"] == str(fact)
    assert result["evidence"][0]["generation_id"] == corpus.generation
    assert result["evidence"][0]["confidence"] is None
    wiki = await read_assets(corpus.member, manifest, "wiki", "Acme")
    assert wiki["items"][0]["revision_id"] == str(revision)
    assert wiki["evidence"] == []  # a Wiki summary never masquerades as primary evidence
    async with corpus.pool.acquire() as c:
        await c.execute("UPDATE knowledge_fact_assertions SET status='withdrawn',updated_at=now() WHERE id=$1", fact)
    assert (await read_assets(corpus.member, manifest, "facts", ""))["items"] == []
    assert (await read_assets(corpus.member, manifest, "wiki", ""))["items"] == []


async def test_native_service_attempts_preserve_provider_usage(corpus, monkeypatch):
    from unittest.mock import AsyncMock

    import httpx

    from open_deep_research.agentscope_runtime import gateway, service_models
    from open_deep_research.models.catalog import ModelCatalogEntry

    for name in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(service_models.LiteLLMModelCatalogClient, "load", AsyncMock(return_value={
        "fixture": ModelCatalogEntry(model_name="fixture", context_window=32000, max_output_tokens=4096,
                                     input_cost_per_token=0.00001, output_cost_per_token=0.00002)}))
    requests = []

    class FixtureModel(gateway.LiteLLMChatModel):
        def __init__(self, **kwargs):
            def reply(request):
                requests.append(request)
                return httpx.Response(200, json={"id": "c1", "object": "chat.completion", "created": 1, "model": "fixture",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}})
            kwargs["client_kwargs"]["http_client"] = httpx.AsyncClient(transport=httpx.MockTransport(reply))
            super().__init__(**kwargs)

    monkeypatch.setattr(gateway, "LiteLLMChatModel", FixtureModel)
    query = str(uuid.uuid4())
    async with accounting.knowledge_query(corpus.member, query):
        for operation in ("answer", "repair"):
            assert await service_models.service_text(model="fixture", api_key="fixture-service", base_url="https://fixture.test/v1",
                system="system", prompt="question", operation=operation) == "ok"
    usage = await accounting.query_usage(query, corpus.member)
    assert len(requests) == 2 and usage["calls"] == {"answer": 1, "repair": 1}
    assert usage["reported"] == {"input_tokens": 6, "output_tokens": 4}
    assert usage["unknown_usage_attempts"] == 0 and usage["cost"] == 140


async def test_answer_repair_usage_is_saved_in_the_query_ledger(corpus, monkeypatch):
    from open_deep_research.knowledge import answer

    async def model(question, evidence, *, operation="answer"):
        attempt = await accounting.reserve_attempt(operation, "fixture")
        await accounting.settle_attempt(attempt, usage={"input_tokens": 5, "output_tokens": 2})
        if operation == "answer":
            raise answer.AnswerUnavailableError("answer_invalid_response")
        return {"answer": "Annual price [1]", "support": "sufficient",
                "citations": [{"marker": "[1]", "segment_ids": [evidence[0]["segment_id"]]}]}

    monkeypatch.setattr(answer, "_call_answer_model", model)
    result = await answer.answer_question(search_service.SearchRequest(owner_id=corpus.member,
        query="Acme", document_ids=[corpus.document]))
    assert result["status"] == "answered" and result["usage"]["calls"] == {"answer": 1, "repair": 1}
    async with corpus.pool.acquire() as c:
        digest = await c.fetchval("SELECT result_digest FROM knowledge_queries WHERE id=$1::uuid", result["query_id"])
    assert json.loads(digest)["usage"] == result["usage"]


async def test_as_of_freezes_the_wiki_revision_at_the_requested_date(corpus):
    from open_deep_research.knowledge.research_assets import read_assets

    async with corpus.pool.acquire() as c:
        await c.execute("UPDATE research_document_generations SET published_at='2020-01-01' WHERE id=$1::uuid", corpus.generation)
        page = await c.fetchval("INSERT INTO knowledge_pages(knowledge_base_id,title) VALUES($1::uuid,'Pricing history') RETURNING id", corpus.base)
        revisions = []
        for number, published in [(1, '2020-01-02'), (2, '2020-02-02')]:
            revision = await c.fetchval("""INSERT INTO knowledge_page_revisions(page_id,revision_number,blocks,status,published_at)
                VALUES($1,$2,'[]','published',$3::text::timestamptz) RETURNING id""", page, number, published)
            revisions.append(str(revision))
            await c.execute("""INSERT INTO knowledge_page_citations(revision_id,block_id,citation_type,document_id,generation_id)
                VALUES($1,'source','document',$2::uuid,$3::uuid)""", revision, corpus.document, corpus.generation)
        await c.execute("UPDATE knowledge_pages SET published_revision_id=$2::uuid WHERE id=$1", page, revisions[-1])
    selection = SourceSelection.model_validate({"mode": "documents", "sources": [{"type": "knowledge_base", "id": corpus.base}],
        "retrieval": {"version_mode": "as_of", "as_of_published": "2020-01-10"}})
    manifest = (await prepare_knowledge_sources(corpus.member, selection))["knowledge_manifest"]
    assert manifest["assets"]["wiki"] == [{"id": revisions[0], "page_id": str(page)}]
    assert (await read_assets(corpus.member, manifest, "wiki", ""))["items"][0]["revision_id"] == revisions[0]


async def test_sync_http_rejects_source_and_document_from_another_base_before_fetch(corpus, monkeypatch):
    from dataclasses import replace
    from unittest.mock import AsyncMock

    import httpx
    from fastapi import FastAPI

    from open_deep_research.knowledge import batch_router, network, sync
    from security.rbac.dependencies import get_current_principal
    from security.rbac.permissions import DOCUMENT_WRITE_OWN
    from tests.auth_helpers import research_principal

    async with corpus.pool.acquire() as c:
        other = str(await c.fetchval("""INSERT INTO knowledge_bases(owner_id,name,description,workspace_id,visibility,created_by)
            VALUES($1::uuid,'Other base','',$2,'team',$1::uuid) RETURNING id""", corpus.owner, corpus.workspace))
    app = FastAPI()
    app.include_router(batch_router.router)
    principal = research_principal(corpus.owner)
    app.dependency_overrides[get_current_principal] = lambda: replace(principal, permissions=principal.permissions | {DOCUMENT_WRITE_OWN.code})
    fetch = AsyncMock(side_effect=AssertionError("network must not run"))
    monkeypatch.setattr(sync.WebAdapter, "discover_changes", fetch)
    monkeypatch.setattr(network, "sync_authorizer", lambda _roles: AsyncMock())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            payload = {"document_id": corpus.document, "url": "https://example.com/pricing"}
            response = await client.post(f"/knowledge-bases/{other}/sync-sources", json=payload)
            assert response.status_code == 403
            response = await client.post(f"/knowledge-bases/{corpus.base}/sync-sources", json=payload)
            assert response.status_code == 201
            source = response.json()["id"]
            response = await client.post(f"/knowledge-bases/{other}/sync-sources/{source}/refresh")
            assert response.status_code == 403
        fetch.assert_not_awaited()
    finally:
        async with corpus.pool.acquire() as c:
            await c.execute("DELETE FROM knowledge_bases WHERE id=$1::uuid", other)
