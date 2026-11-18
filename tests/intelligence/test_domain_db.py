import asyncio
from types import SimpleNamespace
from datetime import datetime, UTC
from uuid import uuid4

import httpx
import pytest

from open_deep_research.intelligence import budget
from open_deep_research.intelligence.authorization import principal_for_grant
from open_deep_research.intelligence.contracts import IntelligenceError, VersionedCommand, ImpactAssessment
from open_deep_research.intelligence.store import record, enqueue, encode
from open_deep_research.intelligence.observations import capture, get_snapshot, read_document
from open_deep_research.intelligence.assessment import assess
from open_deep_research.intelligence.coordinator import Coordinator
from open_deep_research.intelligence.worker import claim

pytestmark = [pytest.mark.asyncio, pytest.mark.db]

SEQUENCES = ["A", "AA", "AB", "ABA", "ABB", "AAB", "ABBA", "ABC", "ABCA", "AABBCC",
             "AAAA", "ABAB", "ABCBA", "ABCABC", "ABCCBA", "AAABBB", "ABBBBA", "ACACA", "ABCDA", "ABCDDCBA"]


async def capture_sequence(domain, sequence, monkeypatch):
    from open_deep_research.intelligence import observations
    client_type = httpx.AsyncClient
    content = {"current": "A"}
    def respond(request):
        tag = content["current"]
        if request.headers.get("if-none-match") == tag:
            return httpx.Response(304, headers={"etag": tag})
        return httpx.Response(200, headers={"etag": tag, "content-type": "text/html"},
            text=f"<html><main><p>Plan {tag} costs USD {ord(tag)} per user per month, with all published conditions included.</p></main></html>")
    monkeypatch.setattr(observations, "httpx", SimpleNamespace(AsyncClient=lambda **kw: client_type(transport=httpx.MockTransport(respond))))
    async def allow(_url):
        return None
    monkeypatch.setattr(observations, "sync_authorizer", lambda roles: allow)
    source = record(await domain.pool.fetchrow("SELECT * FROM intelligence_sources WHERE plan_id=$1::uuid", domain.plan["id"]))
    for index, value in enumerate(sequence):
        content["current"] = value
        async with domain.pool.acquire() as c:
            job_id = await enqueue(c, domain.plan["id"], domain.actor, "capture", f"test:{source['id']}:{index}", {"source_id": source["id"]})
        job = record(await domain.pool.fetchrow("UPDATE intelligence_jobs SET status='running',attempts=1,lease_until=now()+interval '2 minutes' WHERE id=$1 RETURNING *", job_id))
        await capture(job, store=domain.store)
        await domain.pool.execute("UPDATE intelligence_jobs SET status='completed' WHERE id=$1", job_id)
    return source["id"]


@pytest.mark.parametrize("sequence", SEQUENCES)
async def test_source_timeline_no_duplicate_and_return_to_old_content(domain, monkeypatch, sequence):
    source_id = await capture_sequence(domain, sequence, monkeypatch)
    expected = 1 + sum(left != right for left, right in zip(sequence, sequence[1:]))
    rows = await domain.pool.fetch("SELECT * FROM intelligence_snapshots WHERE source_id=$1::uuid ORDER BY sequence", source_id)
    events = await domain.pool.fetch("SELECT * FROM intelligence_events WHERE source_id=$1::uuid ORDER BY sequence", source_id)
    assert len(rows) == len(events) == expected
    assert [r["sequence"] for r in rows] == list(range(1, expected + 1))
    state = await domain.pool.fetchrow("SELECT * FROM intelligence_sources WHERE id=$1::uuid", source_id)
    assert state["next_run_at"] > datetime.now(UTC)
    if sequence[-1] == sequence[0] and expected > 1:
        assert rows[0]["content_sha256"] == rows[-1]["content_sha256"] and rows[0]["id"] != rows[-1]["id"]


async def test_grant_is_session_independent_but_rechecks_authorization_version(domain):
    principal, _, _ = await principal_for_grant(domain.plan["authorization_id"], store=domain.store)
    assert principal.session_id is None and principal.user_id == domain.actor
    await domain.pool.execute("UPDATE iam_users SET authz_version=authz_version+1 WHERE id=$1::uuid", domain.actor)
    with pytest.raises(IntelligenceError, match="reauthorization"):
        await principal_for_grant(domain.plan["authorization_id"], store=domain.store)


async def test_pause_keeps_current_authority_revoke_invalidates_it(domain):
    paused = await domain.store.control(domain.actor, domain.plan["id"], VersionedCommand(expected_version=domain.plan["version"], command_id="pause"), "pause")
    await principal_for_grant(domain.plan["authorization_id"], store=domain.store)
    await domain.store.control(domain.actor, domain.plan["id"], VersionedCommand(expected_version=paused["version"], command_id="revoke"), "revoke")
    with pytest.raises(IntelligenceError, match="revoked"):
        await principal_for_grant(domain.plan["authorization_id"], store=domain.store)


async def test_budget_concurrency_unknown_and_idempotent_settlement(domain):
    async def reserve(key):
        async with domain.pool.acquire() as c, c.transaction():
            return await budget.reserve(c, domain.plan, key, 3_000_000)
    keys = ["budget:" + str(uuid4()) for _ in range(2)]
    results = await asyncio.gather(*(reserve(key) for key in keys), return_exceptions=True)
    assert sum(isinstance(r, IntelligenceError) for r in results) == 1
    winner = keys[next(i for i, r in enumerate(results) if not isinstance(r, BaseException))]
    async with domain.pool.acquire() as c, c.transaction():
        await budget.settle(c, winner, None)
    row = await domain.pool.fetchrow("SELECT * FROM intelligence_budget_periods WHERE plan_id=$1::uuid", domain.plan["id"])
    assert row["reserved"] == 3_000_000
    for _ in range(2):
        async with domain.pool.acquire() as c, c.transaction():
            await budget.settle(c, winner, 12345)
    row = await domain.pool.fetchrow("SELECT * FROM intelligence_budget_periods WHERE plan_id=$1::uuid", domain.plan["id"])
    assert row["reserved"] == 0 and row["settled"] == 12345


async def test_observe_records_proposal_and_does_not_dispatch(domain, monkeypatch):
    await capture_sequence(domain, "AB", monkeypatch)
    await domain.pool.execute("UPDATE intelligence_events SET available_at=now(),create_time=now()-interval '10 seconds' WHERE plan_id=$1::uuid", domain.plan["id"])
    coordinator = Coordinator(None, domain.store)
    await coordinator.batch_events()
    job = record(await domain.pool.fetchrow("UPDATE intelligence_jobs SET status='running',attempts=1,lease_until=now()+interval '2 minutes' WHERE plan_id=$1::uuid AND kind='assess' RETURNING *", domain.plan["id"]))
    async def fixture(payload):
        return ImpactAssessment(decision="research", reason="Price changed", question="Verify the new price",
            evidence_snapshot_ids=[change["snapshot_id"] for change in payload["changes"]])
    result = await assess(job, store=domain.store, evaluate=fixture)
    async def must_not_call(_payload):
        raise AssertionError("committed assessment must replay without billing another model call")
    assert await assess(job, store=domain.store, evaluate=must_not_call) == result
    item, _ = await domain.store.item(result["item_id"])
    assert item["status"] == "proposed" and item["run_id"] is None
    decision = await domain.pool.fetchrow("SELECT * FROM intelligence_decisions WHERE id=$1::uuid", result["decision_id"])
    assert decision["would_start"]
    await coordinator.dispatch()  # None host is safe: observe never dispatches.


async def test_scope_and_artifact_integrity(domain, monkeypatch):
    from open_deep_research.knowledge.authz import AuthorizationError
    sid = await capture_sequence(domain, "A", monkeypatch)
    row = await domain.pool.fetchrow("SELECT * FROM intelligence_snapshots WHERE source_id=$1::uuid", sid)
    snapshot = await get_snapshot(str(row["id"]), domain.actor, store=domain.store)
    from open_deep_research.intelligence.observations import artifact
    with pytest.raises(AuthorizationError):
        await get_snapshot(snapshot["id"], str(uuid4()), store=domain.store)
    (artifact(snapshot) / "raw.bin").write_bytes(b"changed")
    with pytest.raises(IntelligenceError, match="integrity"):
        read_document(snapshot)


async def test_dispatch_keeps_trigger_versions_when_latest_source_has_advanced(domain, monkeypatch):
    source = await capture_sequence(domain, "ABC", monkeypatch)
    event = record(await domain.pool.fetchrow("SELECT * FROM intelligence_events WHERE source_id=$1::uuid AND sequence=2", source))
    item = record(await domain.pool.fetchrow("""INSERT INTO intelligence_items(plan_id,authorization_id,owner_id,question,status)
        VALUES($1::uuid,$2::uuid,$3::uuid,'Explain change B','queued') RETURNING *""", domain.plan["id"], domain.plan["authorization_id"], domain.actor))
    await domain.pool.execute("INSERT INTO intelligence_item_events(item_id,event_id) VALUES($1::uuid,$2::uuid)", item["id"], event["id"])
    _, manifest = await Coordinator(None, domain.store).freeze_request(item, domain.plan)
    ids = {row["id"] for row in manifest["snapshots"]}
    assert {event["previous_snapshot_id"], event["snapshot_id"]} <= ids
    assert len(ids) == 3  # Current C supplements, never replaces trigger A -> B.
    assert manifest["events"][0]["id"] == event["id"]
