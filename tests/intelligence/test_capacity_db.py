"""Capacity/control-plane acceptance with real SQL and a waiting mock executor."""

import asyncio
import json
from datetime import datetime, timedelta, UTC
import time
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from open_deep_research.api.native_runs import idempotent_run_id
from open_deep_research.intelligence.contracts import PlanCreate, PlanBudget, SourceInput, Activate
from open_deep_research.intelligence.coordinator import Coordinator
from open_deep_research.intelligence.router import router, read, write
from open_deep_research.intelligence.store import record

pytestmark = [pytest.mark.asyncio, pytest.mark.db]


async def test_twenty_plans_two_hundred_sources_and_responsive_controls(domain, monkeypatch):
    plans, tasks, active = [], [], set()
    release = asyncio.Event()
    try:
        # Exclude the fixture's seed plan from this independently counted load.
        await domain.pool.execute("UPDATE intelligence_plans SET status='paused' WHERE id=$1::uuid", domain.plan["id"])
        for index in range(20):
            body = PlanCreate(workspace_id=domain.workspace, knowledge_base_id=domain.base, name=f"Capacity {index}",
                objective="Track material pricing changes", sources=[SourceInput(url=f"https://example.com/{index}/{n}") for n in range(10)],
                budget=PlanBudget(run_micro_usd=1_000_000, month_micro_usd=4_000_000))
            plan = await domain.store.create(domain.actor, body)
            plan = await domain.store.activate(domain.principal, plan["id"], Activate(expected_version=1,
                command_id=str(uuid4()), mode="auto", expires_at=datetime.now(UTC)+timedelta(days=1)))
            plans.append(plan)

        async def execute(identity):
            active.add(identity)
            try:
                await release.wait()
            finally:
                active.discard(identity)

        class Executor:
            async def create_automated(self, request, **kwargs):
                identity = idempotent_run_id(domain.actor, f"intelligence:{kwargs['item_id']}:{kwargs['execution_number']}")
                tasks.append(asyncio.create_task(execute(identity)))
                return identity

        coordinator = Coordinator(Executor(), domain.store)
        for _ in range(4):
            await coordinator.schedule()
        ids = [p["id"] for p in plans]
        count = await domain.pool.fetchval("SELECT count(*) FROM intelligence_jobs WHERE plan_id=ANY($1::uuid[]) AND kind='capture'", ids)
        assert count == 200
        await coordinator.schedule()
        assert await domain.pool.fetchval("SELECT count(*) FROM intelligence_jobs WHERE plan_id=ANY($1::uuid[]) AND kind='capture'", ids) == count
        from open_deep_research.intelligence import observations, assessment, worker
        from open_deep_research.intelligence.contracts import ImpactAssessment
        http_client = httpx.AsyncClient
        def respond(request):
            return httpx.Response(200, headers={"content-type": "text/html"},
                text="<main>The plan costs USD 65 per user per month, including all published usage conditions.</main>")
        monkeypatch.setattr(observations, "httpx", SimpleNamespace(AsyncClient=lambda **kwargs: http_client(transport=httpx.MockTransport(respond))))
        async def allowed(_url):
            return None
        monkeypatch.setattr(observations, "sync_authorizer", lambda _roles: allowed)
        async def model(payload):
            return ImpactAssessment(decision="research", reason="Establish the requested baseline", question="Verify pricing conditions",
                evidence_snapshot_ids=[change["snapshot_id"] for change in payload["changes"]])
        async def handle(task, store):
            return await observations.capture(task, store=store) if task["kind"] == "capture" else await assessment.assess(task, store=store, evaluate=model)
        async def drain():
            for _ in range(30):
                if not any(await asyncio.gather(*(worker.run_one(domain.store, handler=handle) for _ in range(8)))):
                    return
            raise AssertionError("fixture work must finish within its bounded queue")
        await drain()
        assert await domain.pool.fetchval("SELECT count(*) FROM intelligence_events WHERE plan_id=ANY($1::uuid[])", ids) == 200
        await asyncio.sleep(5.1)  # Real debounce, no synthetic event timestamps.
        await coordinator.batch_events()
        await drain()
        started = time.perf_counter()
        await coordinator.dispatch()
        await coordinator.dispatch()
        await asyncio.sleep(0)
        assert len(active) == 4 and time.perf_counter() - started < 60
        dispatch_delays = await domain.pool.fetch("SELECT extract(epoch FROM update_time-create_time)::float AS delay FROM intelligence_items WHERE plan_id=ANY($1::uuid[]) AND status='running'", ids)
        assert max(row["delay"] for row in dispatch_delays) < 60
        event_delays = await domain.pool.fetch("""SELECT extract(epoch FROM i.update_time-min(e.create_time))::float AS delay
            FROM intelligence_items i JOIN intelligence_item_events ie ON ie.item_id=i.id JOIN intelligence_events e ON e.id=ie.event_id
            WHERE i.plan_id=ANY($1::uuid[]) AND i.status='running' GROUP BY i.id""", ids)
        assert max(row["delay"] for row in event_delays) < 60
        tasks.append(asyncio.create_task(execute("manual-research")))
        await asyncio.sleep(0)
        assert len(active) == 5

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[read.dependency] = lambda: domain.principal
        app.dependency_overrides[write.dependency] = lambda: domain.principal
        item = record(await domain.pool.fetchrow("SELECT * FROM intelligence_items WHERE plan_id=ANY($1::uuid[]) AND status='running' LIMIT 1", ids))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            times = []
            for index in range(20):
                start = time.perf_counter()
                response = await client.post(f"/intelligence/items/{item['id']}/actions", json={
                    "expected_version": item["version"], "command_id": f"feedback-{index}", "action": "feedback", "reason": "Focus on existing customer conditions"})
                times.append(time.perf_counter()-start)
                assert response.status_code == 200, response.text
                item["version"] = response.json()["version"]
            assert max(times) < 1
            start = time.perf_counter()
            response = await client.post(f"/intelligence/items/{item['id']}/actions", json={
                "expected_version": item["version"], "command_id": "cancel-during-capacity", "action": "cancel", "reason": "Stop this research"})
            cancel_seconds = time.perf_counter()-start
            assert response.status_code == 200 and cancel_seconds < 1
            health = await client.get("/intelligence/health")
            assert health.status_code == 200, health.text
            assert health.json()["metrics"]["queued_research"] == 16
            (domain.tmp_path / "capacity.json").write_text(json.dumps({"plans": 20, "sources": 200,
                "mock_active_research": 5, "dispatch_max_seconds": max(row["delay"] for row in dispatch_delays),
                "event_to_dispatch_max_seconds": max(row["delay"] for row in event_delays),
                "feedback_p95_seconds": sorted(times)[18], "cancel_seconds": cancel_seconds}), encoding="utf-8")
    finally:
        release.set()
        await asyncio.gather(*tasks)
        for plan in plans:
            await domain.pool.execute("UPDATE intelligence_plans SET status='archived' WHERE id=$1::uuid", plan["id"])
            await domain.pool.execute("UPDATE intelligence_jobs SET status='completed' WHERE plan_id=$1::uuid AND status IN ('queued','running')", plan["id"])
            await domain.pool.execute("UPDATE intelligence_items SET status='cancelled' WHERE plan_id=$1::uuid AND status IN ('queued','running')", plan["id"])
