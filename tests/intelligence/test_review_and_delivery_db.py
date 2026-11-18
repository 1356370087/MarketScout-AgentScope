"""Actual PostgreSQL transitions with deterministic parsing and HTTP peers."""

import hashlib
from uuid import uuid4

import httpx
import pytest

from open_deep_research.intelligence.store import record, encode, enqueue
from open_deep_research.intelligence.contracts import ItemReview, IntelligenceError
from open_deep_research.intelligence.observations import artifact, read_document, get_snapshot
from open_deep_research.intelligence.review import prepare_sources, prepare_knowledge, review_item
from test_domain_db import capture_sequence

pytestmark = [pytest.mark.asyncio, pytest.mark.db]


async def job(domain, kind, payload):
    if kind in {"prepare_sources", "prepare_knowledge"}:
        payload = {**payload, "run_sequence": await domain.pool.fetchval("SELECT run_sequence FROM intelligence_items WHERE id=$1::uuid", payload["item_id"])}
    async with domain.pool.acquire() as c:
        identity = await enqueue(c, domain.plan["id"], domain.actor, kind, "test:" + str(uuid4()), payload)
    return record(await domain.pool.fetchrow("UPDATE intelligence_jobs SET status='running',attempts=1,lease_until=now()+interval '2 minutes' WHERE id=$1 RETURNING *", identity))


@pytest.mark.parametrize("reviewer_kind", ["owner", "manager"])
async def test_review_requires_published_original_evidence_and_human_publication(domain, monkeypatch, reviewer_kind):
    reviewer = domain.actor
    if reviewer_kind == "manager":
        reviewer = str(uuid4())
        await domain.pool.execute("UPDATE knowledge_workspaces SET kind='team' WHERE id=$1::uuid", domain.workspace)
        await domain.pool.execute("INSERT INTO iam_users(id,email,email_normalized,status,authz_version) VALUES($1::uuid,$2,$2,'active',1)", reviewer, reviewer + "@example.invalid")
        await domain.pool.execute("INSERT INTO knowledge_workspace_members(workspace_id,user_id,role) VALUES($1::uuid,$2::uuid,'member')", domain.workspace, reviewer)
        await domain.pool.execute("INSERT INTO knowledge_base_members(knowledge_base_id,user_id,role) VALUES($1::uuid,$2::uuid,'manager')", domain.base, reviewer)
    source = await capture_sequence(domain, "A", monkeypatch)
    snapshot = record(await domain.pool.fetchrow("SELECT * FROM intelligence_snapshots WHERE source_id=$1::uuid", source))
    quote = read_document(snapshot).markdown
    evidence = {"evidence_id": "verified-price", "claim": "The price is USD 65.", "supporting_excerpt": quote,
                "source_url": "https://example.com/pricing", "observation_snapshot_id": snapshot["id"]}
    item_id = str(uuid4())
    await domain.pool.execute("""INSERT INTO intelligence_items(id,plan_id,authorization_id,owner_id,question,status,quality_status,result,manifest)
        VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,'Verify pricing','awaiting_review','passed',$5::jsonb,$6::jsonb)""",
        item_id, domain.plan["id"], domain.plan["authorization_id"], domain.actor,
        encode({"report": "The price is USD 65.", "evidence": [evidence]}), encode({"snapshots": [snapshot]}))
    source_job = await job(domain, "prepare_sources", {"item_id": item_id, "reviewer_id": reviewer})
    await prepare_sources(source_job, store=domain.store)
    item, _ = await domain.store.item(item_id)
    doc_id = item["review"]["sources"][snapshot["id"]]["document_id"]
    generation = await domain.pool.fetchval("SELECT id FROM research_document_generations WHERE document_id=$1::uuid ORDER BY create_time DESC LIMIT 1", doc_id)
    from open_deep_research.documents import versioning
    from open_deep_research.documents.chunking import DocumentChunk
    await versioning.complete_generation(str(generation), [DocumentChunk(id=str(uuid4()), ordinal=0, locator="s:1", heading="Pricing",
        text=quote, content_hash=hashlib.sha256(quote.encode()).hexdigest())], [[0.1] * 1536], embedding_model="test", page_count=1, ocr_pages=0)
    knowledge_job = await job(domain, "prepare_knowledge", {"item_id": item_id, "reviewer_id": reviewer})
    with pytest.raises(IntelligenceError, match="not_published"):
        await prepare_knowledge(knowledge_job, store=domain.store)
    from open_deep_research.documents import corrections
    assert await versioning.generation_review_detail(reviewer, doc_id, str(generation)) is not None
    await corrections.apply_corrections(reviewer, doc_id, str(generation), revision=0, metadata_confirmed={"doc_type": "产品定价"})
    await versioning.publish_generation(reviewer, doc_id, str(generation))
    assert str(await domain.pool.fetchval("SELECT actor_id FROM research_document_operations WHERE document_id=$1::uuid AND operation='publish' ORDER BY create_time DESC LIMIT 1", doc_id)) == reviewer
    result = await prepare_knowledge(knowledge_job, store=domain.store)
    # Retrying preparation cannot create a second page/revision.
    assert (await prepare_knowledge(knowledge_job, store=domain.store))["wiki_page_id"] == result["wiki_page_id"]
    from open_deep_research.knowledge.wiki import get_page, publish_page
    page = await get_page(reviewer, result["wiki_page_id"], include_history=True)
    assert page["current_revision"]["status"] == "draft"
    item, _ = await domain.store.item(item_id)
    with pytest.raises(IntelligenceError, match="published_knowledge_required"):
        await review_item(reviewer, item_id, ItemReview(expected_version=item["version"], command_id="premature", action="complete"), store=domain.store)
    published = await publish_page(reviewer, result["wiki_page_id"])
    item, _ = await domain.store.item(item_id)
    complete = ItemReview(expected_version=item["version"], command_id="human-published", action="complete",
                          published_wiki_revision_ids=[(await get_page(reviewer, result["wiki_page_id"]))["published_revision_id"]])
    first = await review_item(reviewer, item_id, complete, store=domain.store)
    assert await review_item(reviewer, item_id, complete, store=domain.store) == first
    assert first["status"] == "completed"
    assert (await domain.store.plan(domain.plan["id"]))["baseline"]["work_item_id"] == item_id
    # A new business question produces its own proposal and preserves the
    # already published page. A delayed old review job cannot attach to it.
    await domain.pool.execute("UPDATE intelligence_items SET run_sequence=2,status='awaiting_review',review='{}'::jsonb WHERE id=$1::uuid", item_id)
    with pytest.raises(IntelligenceError, match="run_changed"):
        await prepare_sources(source_job, store=domain.store)
    new_sources = await job(domain, "prepare_sources", {"item_id": item_id, "reviewer_id": reviewer})
    await prepare_sources(new_sources, store=domain.store)
    new_knowledge = await job(domain, "prepare_knowledge", {"item_id": item_id, "reviewer_id": reviewer})
    second = await prepare_knowledge(new_knowledge, store=domain.store)
    assert second["wiki_page_id"] != result["wiki_page_id"]
    assert (await get_page(reviewer, result["wiki_page_id"]))["current_revision"]["status"] == "published"
    from open_deep_research.intelligence.assessment import assess
    from open_deep_research.intelligence.contracts import ImpactAssessment
    current_plan = await domain.store.plan(domain.plan["id"])
    event_id = await domain.pool.fetchval("SELECT id FROM intelligence_events WHERE plan_id=$1::uuid AND kind='baseline' LIMIT 1", domain.plan["id"])
    assessment_job = await job(domain, "assess", {"event_ids": [str(event_id)], "authorization_id": current_plan["authorization_id"], "plan_version": current_plan["version"]})
    async def evaluate(payload):
        assert payload["published_wiki"] and "USD 65" in payload["published_wiki"][0]["summary"]
        return ImpactAssessment(decision="ignore", reason="No new change against the published baseline",
            affected_wiki_revision_ids=[payload["published_wiki"][0]["revision_id"]])
    assert (await assess(assessment_job, store=domain.store, evaluate=evaluate))["decision_id"]
    from open_deep_research.knowledge import facts
    unit_id = await domain.pool.fetchval("SELECT id FROM research_document_units WHERE generation_id=$1::uuid LIMIT 1", str(generation))
    assertion = await facts.submit_assertion(reviewer, domain.base, entity_name="Competitor", metric="monthly_price",
        value_text="USD 65", value_numeric="65", currency="USD", raw_statement=quote,
        evidence=[{"document_id": doc_id, "generation_id": str(generation), "unit_id": str(unit_id), "excerpt": quote}])
    await facts.publish_assertion(reviewer, assertion["id"])
    await facts.withdraw_assertion(reviewer, assertion["id"])
    kinds = await domain.pool.fetch("SELECT kind FROM intelligence_events WHERE plan_id=$1::uuid AND payload->>'asset_id'=$2 ORDER BY sequence", domain.plan["id"], assertion["id"])
    assert [row["kind"] for row in kinds] == ["fact_published", "fact_withdrawn"]


async def test_feishu_binding_state_is_single_use_and_tokens_are_not_stored(domain, monkeypatch):
    from open_deep_research.intelligence.feishu import start_binding, finish_binding
    from urllib.parse import parse_qs, urlsplit
    monkeypatch.setenv("FEISHU_APP_ID", "test-client")
    monkeypatch.setenv("FEISHU_APP_SECRET", "test-secret")
    monkeypatch.setenv("INTELLIGENCE_PUBLIC_ORIGIN", "https://marketscout.example")
    state = parse_qs(urlsplit((await start_binding(domain.actor, store=domain.store))["url"]).query)["state"][0]
    def respond(request):
        if str(request.url).endswith("/token"):
            return httpx.Response(200, json={"access_token": "not-to-be-stored", "expires_in": 600, "code": 0})
        return httpx.Response(200, json={"code": 0, "data": {"open_id": "ou_" + domain.actor, "tenant_key": "tenant-test"}})
    result = await finish_binding(domain.actor, "code", state, store=domain.store, transport=httpx.MockTransport(respond))
    assert result == {"bound": True, "enabled": False}
    row = record(await domain.pool.fetchrow("SELECT * FROM intelligence_feishu_bindings WHERE user_id=$1::uuid", domain.actor))
    assert "not-to-be-stored" not in encode(row)
    with pytest.raises(IntelligenceError, match="state_invalid"):
        await finish_binding(domain.actor, "code", state, store=domain.store, transport=httpx.MockTransport(respond))


async def test_notification_unknown_outcome_is_not_resent(domain, monkeypatch):
    from open_deep_research.intelligence import feishu
    monkeypatch.setenv("FEISHU_APP_ID", "test-client")
    monkeypatch.setenv("FEISHU_NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setenv("FEISHU_APP_SECRET", "test-secret")
    monkeypatch.setenv("INTELLIGENCE_PUBLIC_ORIGIN", "https://marketscout.example")
    monkeypatch.setattr(feishu, "_token", None)
    await domain.pool.execute("INSERT INTO intelligence_feishu_bindings(user_id,app_id,tenant_key,open_id,enabled) VALUES($1::uuid,'test-client','t',$2,true)", domain.actor, "o_" + domain.actor)
    nid = await domain.pool.fetchval("INSERT INTO intelligence_notifications(plan_id,recipient_id,business_key,kind) VALUES($1::uuid,$2::uuid,$3,'review_ready') RETURNING id", domain.plan["id"], domain.actor, str(uuid4()))
    task = await job(domain, "notify", {"notification_id": str(nid)})
    sent = []
    def respond(request):
        if str(request.url).endswith("/token"):
            return httpx.Response(200, json={"access_token": "test-token", "expires_in": 600})
        sent.append(request)
        raise httpx.ReadTimeout("unknown outcome", request=request)
    for _ in range(2):
        with pytest.raises(IntelligenceError, match="outcome_unknown"):
            await feishu.deliver(task, store=domain.store, transport=httpx.MockTransport(respond))
    assert len(sent) == 1
    assert await domain.pool.fetchval("SELECT delivery_status FROM intelligence_notifications WHERE id=$1", nid) == "unknown"
