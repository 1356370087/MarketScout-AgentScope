"""Leased observation, assessment and delivery jobs; no second research engine."""

import argparse
import asyncio
import logging
import time
from contextlib import AsyncExitStack
from uuid import uuid5, NAMESPACE_URL

import httpx

from open_deep_research.documents.database import close_document_pool
from open_deep_research.knowledge import authz
from open_deep_research.platform import workers
from . import budget
from .contracts import IntelligenceError, DeferredJob, retry_after_seconds
from .settings import settings
from .store import Store, record, encode, notify
from .observations import assert_job

log = logging.getLogger(__name__)
KINDS = ["capture", "assess", "prepare_sources", "prepare_knowledge", "notify"]


async def claim(store):
    pool = await store.pool()
    return record(await pool.fetchrow("""UPDATE intelligence_jobs SET status='running',attempts=attempts+1,
        lease_until=now()+interval '2 minutes',started_at=coalesce(started_at,now()),update_time=now() WHERE id=(
          SELECT id FROM intelligence_jobs WHERE kind=ANY($1::text[]) AND available_at<=now()
          AND (status='queued' OR (status='running' AND lease_until<now()))
          ORDER BY available_at,create_time FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *""", KINDS))


async def execute(job, store):
    if job["kind"] == "capture":
        from .observations import capture
        return await capture(job, store=store)
    if job["kind"] == "assess":
        from .assessment import assess
        return await assess(job, store=store)
    if job["kind"] in {"prepare_sources", "prepare_knowledge"}:
        from .review import prepare_sources, prepare_knowledge
        return await (prepare_sources if job["kind"] == "prepare_sources" else prepare_knowledge)(job, store=store)
    from .feishu import deliver
    return await deliver(job, store=store)


async def failed(job, store, exc):
    pool = await store.pool()
    plan = await store.plan(job["plan_id"])
    # GET failures are safe to retry. Model calls and external writes have
    # their own receipts and must never be inferred unexecuted from a timeout.
    code = exc.code if isinstance(exc, IntelligenceError) else type(exc).__name__
    from open_deep_research.platform.accounting import UncertainServiceCall
    if isinstance(exc, UncertainServiceCall):
        code = "impact_model_result_unresolved"
    limited = code.startswith(("observation_rate_limited", "feishu_rate_limited"))
    retry = (job["kind"] == "capture" and isinstance(exc, (httpx.TransportError, httpx.HTTPStatusError)) or limited) and job["attempts"] < 3
    status = "queued" if retry else "failed"
    if "unknown" in code or "unresolved" in code:
        status = "unknown"
    delay = min(3600, 30 * 2 ** job["attempts"])
    if limited:
        delay = retry_after_seconds(code.split(":", 1)[-1])
    actual = await budget.service_cost(job["id"]) if job["kind"] == "assess" else None
    async with pool.acquire() as c, c.transaction():
        await assert_job(c, job)
        expected_grant = job["payload"].get("authorization_id") or plan["authorization_id"]
        if code == "automation_authorization_superseded" and expected_grant != plan["authorization_id"]:
            await c.execute("UPDATE intelligence_jobs SET status='completed',error=$2,lease_until=NULL,completed_at=now() WHERE id=$1::uuid", job["id"], code)
            if job["kind"] == "assess":
                await budget.settle(c, "impact:" + job["id"], actual)
                await c.execute("UPDATE intelligence_events SET status='pending' WHERE id=ANY($1::uuid[]) AND status='assessing'", job["payload"]["event_ids"])
            return
        await c.execute("""UPDATE intelligence_jobs SET status=$3,error=$4,lease_until=NULL,
            available_at=now()+make_interval(secs=>$5),update_time=now() WHERE id=$1::uuid AND attempts=$2""",
            job["id"], job["attempts"], status, code, delay)
        if job["kind"] == "capture":
            await c.execute("""UPDATE intelligence_sources SET failures=greatest(failures+1,$3),error=$2,
                last_checked_at=now(),update_time=now(),next_run_at=CASE WHEN $4 THEN next_run_at ELSE NULL END
                WHERE id=$1::uuid""", job["payload"]["source_id"], code, 1 if retry else 3, retry)
        if job["kind"] == "assess":
            await budget.settle(c, "impact:" + job["id"], actual)
            item_id = str(uuid5(NAMESPACE_URL, "impact-failed:" + job["id"]))
            await c.execute("""INSERT INTO intelligence_items(id,plan_id,authorization_id,owner_id,question,status,reason,created_at)
                VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,'proposed',$6,$4::uuid) ON CONFLICT(id) DO NOTHING""",
                item_id, plan["id"], job["payload"]["authorization_id"], plan["owner_id"], plan["objective"], code)
            for event_id in job["payload"]["event_ids"]:
                await c.execute("INSERT INTO intelligence_item_events(item_id,event_id) VALUES($1::uuid,$2::uuid) ON CONFLICT(event_id) DO NOTHING", item_id, event_id)
            await c.execute("UPDATE intelligence_events SET status='needs_review' WHERE id=ANY($1::uuid[])", job["payload"]["event_ids"])
        if not retry and job["kind"] != "notify":
            await notify(c, plan, "source_or_job_failed", "job-failure:" + job["id"] + ":" + code)
        if code.startswith(("automation_authorization_", "automation_owner_", "automation_research_permission_")) or "requires_authorization" in code or (job["kind"] == "assess" and status == "unknown"):
            await c.execute("""UPDATE intelligence_plans SET status='paused',reason=$3,version=version+1,update_time=now()
                WHERE id=$1::uuid AND authorization_id=$2::uuid AND status='active'""", plan["id"], expected_grant, code)


async def run_one(store=None, *, handler=execute):
    store = store or Store()
    job = await claim(store)
    if not job:
        return False
    pool = await store.pool()

    async def heartbeat():
        while True:
            await asyncio.sleep(30)
            changed = await pool.fetchval("""UPDATE intelligence_jobs SET lease_until=now()+interval '2 minutes'
                WHERE id=$1::uuid AND attempts=$2 AND status='running' AND lease_until>now() RETURNING id""", job["id"], job["attempts"])
            if not changed:
                return

    beat = asyncio.create_task(heartbeat())
    try:
        if handler is execute:
            from .authorization import require_managed_accounting
            require_managed_accounting()
        async with workers.job_context("intelligence", job["id"], job["owner_id"]):
            result = await handler(job, store)
        async with pool.acquire() as c, c.transaction():
            await assert_job(c, job)
            await c.execute("UPDATE intelligence_jobs SET status='completed',result=$3::jsonb,lease_until=NULL,error=NULL,completed_at=now(),update_time=now() WHERE id=$1::uuid AND attempts=$2",
                job["id"], job["attempts"], encode(result))
    except asyncio.CancelledError:
        raise  # The lease and receipt survive shutdown; no unknown calls are replayed.
    except DeferredJob:
        async with pool.acquire() as c, c.transaction():
            await assert_job(c, job)
            await c.execute("UPDATE intelligence_jobs SET status='queued',lease_until=NULL,available_at=now()+interval '30 seconds' WHERE id=$1::uuid AND attempts=$2", job["id"], job["attempts"])
    except Exception as exc:
        try:
            await failed(job, store, exc)
        except IntelligenceError as fence:
            if fence.code != "intelligence_job_lease_lost":
                raise
        log.warning("持续情报任务 %s 结束：%s", job["id"], type(exc).__name__)
    finally:
        beat.cancel()
        await asyncio.gather(beat, return_exceptions=True)
    return True


async def main(once=False):
    await workers.initialize("intelligence-worker")
    last_sweep = 0.0
    try:
        while True:
            workers.acknowledge("intelligence-worker")
            if await workers.active_ready() and settings().enabled:
                pool = await Store().pool()
                await pool.execute("""INSERT INTO intelligence_jobs(kind,business_key,status,completed_at,created_at)
                    VALUES('heartbeat','intelligence-worker','completed',now(),'intelligence-worker')
                    ON CONFLICT(business_key) DO UPDATE SET completed_at=now(),update_time=now()""")
                results = await asyncio.gather(*(run_one() for _ in range(8)), return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException):
                        log.error("持续情报 Worker 未就绪：%s", type(result).__name__)
                if time.monotonic() - last_sweep > 3600:
                    from .maintenance import sweep
                    await sweep()
                    last_sweep = time.monotonic()
            if once:
                return
            await asyncio.sleep(settings().poll_seconds)
    finally:
        await close_document_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    asyncio.run(main(parser.parse_args().once))
