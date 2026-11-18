"""Recovery fences, financial reservations and live delivery boundaries."""

from uuid import uuid4

import httpx
import pytest

from open_deep_research.intelligence import budget
from open_deep_research.intelligence.contracts import IntelligenceError, ImpactAssessment, DeferredJob
from open_deep_research.intelligence.store import record, encode
from open_deep_research.intelligence.observations import assert_job, artifact
from open_deep_research.intelligence.assessment import assess
from open_deep_research.intelligence.worker import claim
from open_deep_research.intelligence.service_accounting import DocumentBudgetPort
from test_domain_db import capture_sequence
from test_review_and_delivery_db import job

pytestmark = [pytest.mark.asyncio, pytest.mark.db]


async def test_expired_worker_cannot_commit_after_reclaim(domain):
    first = await job(domain, "capture", {})
    await domain.pool.execute("UPDATE intelligence_jobs SET lease_until=now()-interval '1 second' WHERE id=$1::uuid", first["id"])
    second = await claim(domain.store)
    assert second["id"] == first["id"] and second["attempts"] == 2
    async with domain.pool.acquire() as c:
        with pytest.raises(IntelligenceError, match="lease_lost"):
            await assert_job(c, first)
        await assert_job(c, second)


async def assessment_job(domain, monkeypatch):
    await capture_sequence(domain, "AB", monkeypatch)
    events = await domain.pool.fetch("UPDATE intelligence_events SET status='assessing' WHERE plan_id=$1::uuid RETURNING id", domain.plan["id"])
    return await job(domain, "assess", {"event_ids": [str(row["id"]) for row in events],
        "plan_version": domain.plan["version"], "authorization_id": domain.plan["authorization_id"]})


async def test_published_baseline_change_during_model_call_requeues_without_old_decision(domain, monkeypatch):
    task = await assessment_job(domain, monkeypatch)
    async def change_baseline(payload):
        await domain.pool.execute("UPDATE intelligence_plans SET version=version+1,baseline=$2::jsonb WHERE id=$1::uuid",
                                  domain.plan["id"], encode({"fact_ids": [str(uuid4())]}))
        return ImpactAssessment(decision="ignore", reason="Old baseline appears unchanged")
    assert (await assess(task, store=domain.store, evaluate=change_baseline))["status"] == "policy_changed"
    assert await domain.pool.fetchval("SELECT count(*) FROM intelligence_decisions WHERE job_id=$1::uuid", task["id"]) == 0
    assert await domain.pool.fetchval("SELECT count(*) FROM intelligence_events WHERE plan_id=$1::uuid AND status='pending'", domain.plan["id"]) == 2
    assert await domain.pool.fetchval("SELECT reserved FROM intelligence_budget_periods WHERE plan_id=$1::uuid", domain.plan["id"]) == 0


async def test_model_marker_survives_pause_without_replay(domain, monkeypatch):
    task = await assessment_job(domain, monkeypatch)
    task["result"] = {"model_started": True}
    await domain.pool.execute("UPDATE intelligence_plans SET status='paused' WHERE id=$1::uuid", domain.plan["id"])
    async def forbidden(_):
        raise AssertionError("an unknown paid call cannot run twice")
    with pytest.raises(IntelligenceError, match="result_unresolved"):
        await assess(task, store=domain.store, evaluate=forbidden)


async def test_removed_sources_are_filtered_before_impact_model(domain, monkeypatch):
    task = await assessment_job(domain, monkeypatch)
    await domain.pool.execute("UPDATE intelligence_sources SET is_deleted=true WHERE plan_id=$1::uuid", domain.plan["id"])
    async def forbidden(_):
        raise AssertionError("removed source text must not reach the model")
    result = await assess(task, store=domain.store, evaluate=forbidden)
    assert result["status"] == "outside_current_scope"
    assert await domain.pool.fetchval("SELECT count(*) FROM intelligence_budget_reservations WHERE plan_id=$1::uuid", domain.plan["id"]) == 0


async def test_impact_cannot_introduce_foreign_fact_or_snapshot(domain, monkeypatch):
    task = await assessment_job(domain, monkeypatch)
    async def foreign(_):
        return ImpactAssessment(decision="research", reason="Injected IDs", affected_fact_ids=[str(uuid4())],
                                evidence_snapshot_ids=[str(uuid4())])
    with pytest.raises(IntelligenceError, match="outside_scope"):
        await assess(task, store=domain.store, evaluate=foreign)
    assert await domain.pool.fetchval("SELECT count(*) FROM intelligence_items WHERE plan_id=$1::uuid", domain.plan["id"]) == 0


async def test_document_costs_keep_unknown_reservation_and_settle_only_once(domain):
    port = DocumentBudgetPort({"plan_id": domain.plan["id"], "authorization_id": domain.plan["authorization_id"], "billing_owner_id": domain.actor})
    identity = str(uuid4())
    await port.reserve(identity, 500_000, domain.actor)
    await port.settle(identity, None)
    assert await domain.pool.fetchval("SELECT reserved FROM intelligence_budget_periods WHERE plan_id=$1::uuid", domain.plan["id"]) == 500_000
    await port.settle(identity, 12345)
    await port.settle(identity, 12345)
    row = await domain.pool.fetchrow("SELECT reserved,settled FROM intelligence_budget_periods WHERE plan_id=$1::uuid", domain.plan["id"])
    assert tuple(row) == (0, 12345)
    with pytest.raises(IntelligenceError, match="owner_changed"):
        await port.reserve(str(uuid4()), 100, str(uuid4()))
    await domain.pool.execute("UPDATE iam_users SET authz_version=authz_version+1 WHERE id=$1::uuid", domain.actor)
    with pytest.raises(IntelligenceError, match="reauthorization"):
        await port.reserve(str(uuid4()), 100, domain.actor)


async def test_retention_keeps_pending_events_and_previous_run_inputs(domain, monkeypatch):
    from open_deep_research.intelligence.maintenance import sweep
    sid = await capture_sequence(domain, "ABCA", monkeypatch)
    snapshots = [record(row) for row in await domain.pool.fetch("SELECT * FROM intelligence_snapshots WHERE source_id=$1::uuid ORDER BY sequence", sid)]
    await domain.pool.execute("UPDATE intelligence_plans SET status='paused' WHERE id=$1::uuid", domain.plan["id"])
    await domain.pool.execute("UPDATE intelligence_snapshots SET observed_at=now()-interval '100 days' WHERE source_id=$1::uuid", sid)
    await sweep(domain.store)
    assert all(artifact(row).exists() for row in snapshots)
    await domain.pool.execute("UPDATE intelligence_events SET status='assessed' WHERE plan_id=$1::uuid", domain.plan["id"])
    iid = await domain.pool.fetchval("""INSERT INTO intelligence_items(plan_id,authorization_id,owner_id,question,status)
        VALUES($1::uuid,$2::uuid,$3::uuid,'Past inputs','completed') RETURNING id""", domain.plan["id"], domain.plan["authorization_id"], domain.actor)
    await domain.pool.execute("""INSERT INTO intelligence_item_runs(item_id,run_id,result) VALUES($1,$2,$3::jsonb)""",
        iid, str(uuid4()), encode({"manifest": {"snapshots": [{"id": snapshots[0]["id"]}]}}))
    await sweep(domain.store)
    assert artifact(snapshots[0]).exists() and artifact(snapshots[-1]).exists()
    assert not artifact(snapshots[1]).exists() and not artifact(snapshots[2]).exists()
    assert (await sweep(domain.store))["removed_snapshots"] == 0
    import os, time
    from open_deep_research.intelligence.observations import root
    old_orphan, recent = root() / "ff" / str(uuid4()), root() / "fe" / str(uuid4())
    for directory in (old_orphan, recent):
        directory.mkdir(parents=True)
        (directory / "raw.bin").write_bytes(b"uncommitted capture")
    old = time.time() - 100 * 86400
    os.utime(old_orphan, (old, old))
    assert (await sweep(domain.store))["removed_orphans"] == 1
    assert not old_orphan.exists() and recent.exists() and artifact(snapshots[0]).exists()


async def notification_job(domain, monkeypatch):
    from open_deep_research.intelligence import feishu
    monkeypatch.setenv("FEISHU_APP_ID", "fault-client")
    monkeypatch.setenv("FEISHU_NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setenv("FEISHU_APP_SECRET", "fake-test-secret")
    monkeypatch.setenv("INTELLIGENCE_PUBLIC_ORIGIN", "https://marketscout.example")
    monkeypatch.setattr(feishu, "_token", None)
    await domain.pool.execute("INSERT INTO intelligence_feishu_bindings(user_id,app_id,tenant_key,open_id,enabled) VALUES($1::uuid,'fault-client','t',$2,true)", domain.actor, "o_"+domain.actor)
    nid = await domain.pool.fetchval("INSERT INTO intelligence_notifications(plan_id,recipient_id,business_key,kind) VALUES($1::uuid,$2::uuid,$3,'review_ready') RETURNING id", domain.plan["id"], domain.actor, str(uuid4()))
    return nid, await job(domain, "notify", {"notification_id": str(nid)})


@pytest.mark.parametrize("boundary", ["unbound", "inactive", "app_changed", "workspace_revoked"])
async def test_feishu_checks_current_binding_and_access_before_send(domain, monkeypatch, boundary):
    from open_deep_research.intelligence import feishu
    nid, task = await notification_job(domain, monkeypatch)
    if boundary == "unbound":
        await domain.pool.execute("DELETE FROM intelligence_feishu_bindings WHERE user_id=$1::uuid", domain.actor)
    elif boundary == "inactive":
        await domain.pool.execute("UPDATE iam_users SET status='disabled' WHERE id=$1::uuid", domain.actor)
    elif boundary == "app_changed":
        monkeypatch.setenv("FEISHU_APP_ID", "another-client")
    else:
        await domain.pool.execute("UPDATE knowledge_workspaces SET status='suspended' WHERE id=$1::uuid", domain.workspace)
    def forbidden(_):
        raise AssertionError("must not request credentials or send a message")
    assert (await feishu.deliver(task, store=domain.store, transport=httpx.MockTransport(forbidden)))["status"] == "dropped"


async def test_feishu_rate_limit_reuses_notification_id(domain, monkeypatch):
    from open_deep_research.intelligence import feishu
    nid, task = await notification_job(domain, monkeypatch)
    sent = []
    def respond(request):
        if str(request.url).endswith("/token"):
            return httpx.Response(200, json={"access_token": "test", "expires_in": 600})
        sent.append(request)
        return httpx.Response(429, headers={"retry-after": "5"}) if len(sent) == 1 else httpx.Response(200, json={"code": 0, "data": {"message_id": "accepted"}})
    with pytest.raises(IntelligenceError, match="rate_limited:5"):
        await feishu.deliver(task, store=domain.store, transport=httpx.MockTransport(respond))
    assert (await feishu.deliver(task, store=domain.store, transport=httpx.MockTransport(respond)))["status"] == "sent"
    import json
    assert [json.loads(request.content)["uuid"] for request in sent] == [str(nid), str(nid)]
    assert (await feishu.deliver(task, store=domain.store, transport=httpx.MockTransport(respond)))["status"] == "settled"
    assert len(sent) == 2


async def test_publication_events_commit_with_knowledge_and_preserve_sequence(domain):
    from open_deep_research.intelligence.publications import record_asset_publication
    asset = str(uuid4())
    async with domain.pool.acquire() as c:
        transaction = c.transaction()
        await transaction.start()
        await record_asset_publication(c, domain.base, "wiki_published", asset, str(uuid4()), "rollback", domain.actor)
        await transaction.rollback()
    assert await domain.pool.fetchval("SELECT count(*) FROM intelligence_events WHERE plan_id=$1::uuid", domain.plan["id"]) == 0
    for body in ("A", "B", "A"):
        async with domain.pool.acquire() as c, c.transaction():
            await record_asset_publication(c, domain.base, "wiki_published", asset, str(uuid4()), body, domain.actor)
    rows = await domain.pool.fetch("SELECT sequence,payload->>'content' AS content FROM intelligence_events WHERE plan_id=$1::uuid ORDER BY sequence", domain.plan["id"])
    assert [(r["sequence"], r["content"]) for r in rows] == [(1, "A"), (2, "B"), (3, "A")]
    iid = await domain.pool.fetchval("""INSERT INTO intelligence_items(plan_id,authorization_id,owner_id,question,review)
        VALUES($1::uuid,$2::uuid,$3::uuid,'Origin',$4::jsonb) RETURNING id""", domain.plan["id"], domain.plan["authorization_id"], domain.actor, encode({"wiki_page_id": asset}))
    async with domain.pool.acquire() as c, c.transaction():
        await record_asset_publication(c, domain.base, "wiki_published", asset, str(uuid4()), "Published by this plan", domain.actor)
    last = await domain.pool.fetchrow("SELECT status,origin_item_id FROM intelligence_events WHERE plan_id=$1::uuid ORDER BY sequence DESC LIMIT 1", domain.plan["id"])
    assert last["status"] == "self_published" and last["origin_item_id"] == iid


async def test_manual_request_is_idempotent_in_observe_and_baseline_scope_is_checked(domain):
    from open_deep_research.intelligence.router import ManualRequest, manual_request, BaselineUpdate, link_baseline
    from fastapi import HTTPException
    request = ManualRequest(expected_version=domain.plan["version"], command_id="human-request", question="Check enterprise pricing conditions")
    first = await manual_request(domain.plan["id"], request, domain.principal)
    assert await manual_request(domain.plan["id"], request, domain.principal) == first
    assert first["manual"] and first["status"] == "queued"
    plan = await domain.store.plan(domain.plan["id"])
    with pytest.raises(HTTPException) as error:
        await link_baseline(plan["id"], BaselineUpdate(expected_version=plan["version"], command_id="foreign", fact_ids=[uuid4()]), domain.principal)
    assert error.value.status_code == 403


async def test_external_delivery_switch_defers_without_consuming_receipt(domain, monkeypatch):
    from open_deep_research.intelligence import feishu
    nid, task = await notification_job(domain, monkeypatch)
    monkeypatch.setenv("FEISHU_NOTIFICATIONS_ENABLED", "false")
    def forbidden(_):
        raise AssertionError("external delivery is disabled")
    with pytest.raises(DeferredJob):
        await feishu.deliver(task, store=domain.store, transport=httpx.MockTransport(forbidden))
    assert await domain.pool.fetchval("SELECT delivery_status FROM intelligence_notifications WHERE id=$1", nid) == "pending"


async def test_two_budget_layers_use_one_personal_charge_on_real_postgres(domain, monkeypatch):
    from unittest.mock import AsyncMock
    from security.rbac.database import get_engine
    from open_deep_research.platform import accounting, integration, runtime, quota
    from open_deep_research.platform.store import PlatformStore
    platform = PlatformStore(get_engine())
    monkeypatch.setattr(platform, "limits", AsyncMock(return_value={"monthly_cost_micro_usd": 100}))
    monkeypatch.setattr(quota, "get_store", lambda: platform)
    monkeypatch.setattr(runtime, "enabled", lambda: True)
    monkeypatch.setattr(runtime, "snapshot", lambda: {"document": {"services": {"DOCUMENT_RESEARCH_ENABLED": True,
        "DOCUMENT_STORAGE_DIR": str(domain.tmp_path)}}})
    monkeypatch.setattr(integration, "check_credentials", AsyncMock())
    port = DocumentBudgetPort({"plan_id": domain.plan["id"], "authorization_id": domain.plan["authorization_id"], "billing_owner_id": domain.actor})
    identity = str(uuid4())
    with accounting.scoped_service_budget(port):
        charge = await accounting.begin("test", "prompt", 10, "acceptance", identity=identity, owner=domain.actor, pricing=(1, 2))
        await accounting.complete(charge, {"input_tokens": 5, "output_tokens": 10})
        await accounting.complete(charge, {"input_tokens": 5, "output_tokens": 10})
        with pytest.raises(quota.QuotaExceeded):
            await accounting.begin("test", "prompt", 100, "acceptance", owner=domain.actor, pricing=(1, 2))
        async with domain.pool.acquire() as c, c.transaction():
            await budget.reserve(c, domain.plan, "other:" + identity, 3_999_970)
        with pytest.raises(IntelligenceError, match="monthly_budget_exhausted"):
            await accounting.begin("test", "prompt", 10, "acceptance", owner=domain.actor, pricing=(1, 2))
    personal = await domain.pool.fetchrow("SELECT reserved,settled FROM platform_quota_months WHERE owner_id=$1", domain.actor)
    assert tuple(personal) == (0, 25)
    assert await domain.pool.fetchval("SELECT settled FROM intelligence_budget_periods WHERE plan_id=$1::uuid", domain.plan["id"]) == 25
