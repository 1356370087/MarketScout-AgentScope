"""API-owned coordination of durable work, using the existing NativeRuns host."""

import asyncio
import logging
from uuid import uuid4

from . import budget
from .authorization import principal_for_grant
from .contracts import IntelligenceError, next_check, STRICT_QUALITY
from .settings import settings
from .store import Store, record, encode, enqueue, notify, digest

log = logging.getLogger(__name__)


def quality_result(state):
    review = state.report_product.get("report_review") or {}
    if review.get("type") == "override":
        review = review.get("value") or {}
    assessments = [row.get("assessment", {}).get("handoff", {}) for row in state.findings
                   if row.get("assessment", {}).get("handoff", {}).get("accepted") is True]
    passed = (state.completion_outcome.get("action") == "complete" and bool(assessments)
        and all(row.get("accepted") is True and row.get("evaluation_status") == "evaluated" for row in assessments)
        and review.get("decision") == "pass" and not review.get("skipped") and not review.get("degraded")
        and review.get("status") not in {"skipped", "error", "failed", "degraded"})
    return "passed" if passed else "partial" if state.final_report or state.report_product.get("final_report_draft") else "failed"


def settled_cost(usage):
    if any(usage["reserved"].values()):
        return None
    if "cost_micro_usd" in usage["used"]:
        return usage["used"]["cost_micro_usd"]
    return None if usage["used"].get("model_calls") else 0


class Coordinator:
    def __init__(self, runs, store=None):
        self.runs, self.store = runs, store or Store()

    async def schedule(self):
        pool = await self.store.pool()
        rows = await pool.fetch("""SELECT s.*,p.definition AS plan_definition,p.owner_id,p.authorization_id
            FROM intelligence_sources s JOIN intelligence_plans p ON p.id=s.plan_id
            WHERE s.next_run_at<=now() AND NOT s.is_deleted AND s.failures<3 AND p.status='active' AND s.definition->>'kind'<>'knowledge_events'
            AND NOT EXISTS(SELECT 1 FROM intelligence_jobs j WHERE j.kind='capture'
                AND j.payload->>'source_id'=s.id::text AND j.status IN ('queued','running'))
            ORDER BY s.next_run_at LIMIT 50""")
        for raw in rows:
            source = record(raw)
            definition = raw["plan_definition"]
            if isinstance(definition, str):
                import json
                definition = json.loads(definition)
            async with pool.acquire() as c, c.transaction():
                taken = await c.fetchval("""UPDATE intelligence_sources SET next_run_at=$2
                    WHERE id=$1::uuid AND next_run_at<=now() RETURNING id""", source["id"], next_check(definition["cadence"]))
                if taken:
                    await enqueue(c, source["plan_id"], str(raw["owner_id"]), "capture",
                        "capture:" + source["id"] + ":" + source["next_run_at"],
                        {"source_id": source["id"], "authorization_id": str(raw["authorization_id"])})

    async def batch_events(self):
        pool = await self.store.pool()
        plans = [record(row) for row in await pool.fetch("""SELECT p.* FROM intelligence_plans p WHERE p.status='active'
            AND NOT EXISTS(SELECT 1 FROM intelligence_items i WHERE i.plan_id=p.id AND i.status IN ('queued','running'))
            AND NOT EXISTS(SELECT 1 FROM intelligence_jobs j WHERE j.plan_id=p.id AND j.kind='assess' AND j.status IN ('queued','running'))
            AND EXISTS(SELECT 1 FROM intelligence_events e WHERE e.plan_id=p.id AND e.status='pending'
                GROUP BY e.plan_id HAVING max(e.create_time)<=now()-interval '5 seconds'
                    OR min(e.create_time)<=now()-interval '60 seconds')
            ORDER BY p.update_time LIMIT 25""")]
        for plan in plans:
            async with pool.acquire() as c, c.transaction():
                events = await c.fetch("""UPDATE intelligence_events SET status='assessing'
                    WHERE id IN (SELECT id FROM intelligence_events WHERE plan_id=$1::uuid AND status='pending'
                        AND available_at<=now() ORDER BY create_time FOR UPDATE SKIP LOCKED LIMIT 200) RETURNING id""", plan["id"])
                if events:
                    ids = sorted(str(row["id"]) for row in events)
                    await enqueue(c, plan["id"], plan["owner_id"], "assess", "assess:" + digest([ids, plan["version"]]),
                        {"event_ids": ids, "plan_version": plan["version"], "authorization_id": plan["authorization_id"]})

    async def freeze_request(self, item, plan):
        from open_deep_research.api.contracts import RunRequest
        from open_deep_research.documents.contracts import SourceSelection
        pool = await self.store.pool()
        snapshots = [record(row) for row in await pool.fetch("""SELECT o.* FROM intelligence_sources s
            JOIN intelligence_snapshots o ON o.id=s.snapshot_id WHERE s.plan_id=$1::uuid AND NOT s.is_deleted
            ORDER BY s.id""", plan["id"])]
        events = [record(row) for row in await pool.fetch("""SELECT e.id,e.kind,e.previous_snapshot_id,e.snapshot_id,e.create_time
            FROM intelligence_item_events ie JOIN intelligence_events e ON e.id=ie.event_id
            WHERE ie.item_id=$1::uuid AND e.plan_id=$2::uuid ORDER BY e.create_time""", item["id"], plan["id"])]
        trigger_ids = {event[key] for event in events for key in ("previous_snapshot_id", "snapshot_id") if event[key]}
        trigger_snapshots = [record(row) for row in await pool.fetch("""SELECT o.* FROM intelligence_snapshots o
            JOIN intelligence_sources s ON s.id=o.source_id WHERE o.id=ANY($1::uuid[]) AND o.plan_id=$2::uuid
            AND NOT o.is_deleted AND NOT s.is_deleted""", list(trigger_ids), plan["id"])]
        current_ids = {row["id"] for row in snapshots}
        snapshots.extend(row for row in trigger_snapshots if row["id"] not in current_ids)
        prior_run = plan["baseline"].get("run_id")
        history = record(await pool.fetchrow("""SELECT r.result FROM intelligence_item_runs r JOIN intelligence_items i ON i.id=r.item_id
            WHERE r.run_id=$1 AND i.plan_id=$2::uuid""", prior_run, plan["id"])) if prior_run else None
        if history:
            from open_deep_research.documents.contracts import source_url_identity
            allowed_urls = {source_url_identity(row["url"]) for row in plan["definition"]["sources"] if row["kind"] == "web"}
            allowed_documents = {row["document_id"] for row in plan["definition"]["sources"] if row["kind"] == "knowledge"}
            current_ids = {row["id"] for row in snapshots}
            for old in history["result"].get("manifest", {}).get("snapshots", []):
                allowed = old.get("document_id") in allowed_documents if old.get("generation_id") else source_url_identity(old["metadata"]["url"]) in allowed_urls
                if old["id"] not in current_ids and allowed:
                    snapshots.append(old)
                    current_ids.add(old["id"])
        baseline_facts = [record(row) for row in await pool.fetch("""SELECT id,raw_statement,value_text,value_numeric,unit,currency,data_period,valid_from,valid_until
            FROM knowledge_fact_assertions WHERE id=ANY($1::uuid[]) AND knowledge_base_id=$2::uuid AND status='published'""",
            plan["baseline"].get("fact_ids", []), plan["knowledge_base_id"])]
        baseline_pages = [record(row) for row in await pool.fetch("""SELECT r.id::text AS id,r.blocks FROM knowledge_page_revisions r
            JOIN knowledge_pages p ON p.id=r.page_id WHERE r.id=ANY($1::uuid[]) AND p.knowledge_base_id=$2::uuid AND r.status='published'""",
            plan["baseline"].get("wiki_revision_ids", []), plan["knowledge_base_id"])]
        for page in baseline_pages:
            page["summary"] = "\n".join(str(block.get("content", "")) for block in page.pop("blocks", []))[:2000]
            page["status"] = "published"
        manifest = {"version": 1, "plan_id": plan["id"], "events": events, "snapshots": [
            {key: row[key] for key in ("id", "source_id", "raw_sha256", "content_sha256", "metadata", "observed_at", "document_id", "generation_id")}
            for row in snapshots]}
        manifest["baseline_context"] = {"knowledge_base_id": plan["knowledge_base_id"], "facts": baseline_facts,
            "pages": baseline_pages, "context_only": True, "instruction": "这些是用户已发布的历史判断；与当前事实比较时仍需读取对应冻结快照并校验证据。"}
        scope = [{"type": "url", "url": source["url"]} if source["kind"] == "web" else {"type": "document", "id": source["document_id"]}
                 for source in plan["definition"]["sources"]]
        cfg = {**plan["definition"]["configurable"], **STRICT_QUALITY,
            "max_run_cost_micro_usd": plan["definition"]["budget"]["run_micro_usd"],
            "allow_clarification": False, "enable_human_in_loop": False, "enable_memory": False}
        prompt = ("持续情报研究目标：" + plan["objective"] + "\n本次需要核实：" + item["question"]
            + "\n优先使用 ReadObservation 读取本次冻结的网页观察快照。知识资料使用已授权的文档工具。"
            + "\n输出变化简报：原结论、新证据、变化说明、适用时期与条件、不确定性、建议更新。"
            + "\n不得自行发布知识或扩大来源、预算与动作权限。已有已发布基线：" + encode(plan["baseline"]))
        request = RunRequest(messages=[{"role": "user", "content": prompt}], title=plan["name"][:120] + " · 变化研究",
            configurable=cfg, profile_revision_id=plan["definition"].get("profile_revision_id"),
            source_selection=SourceSelection(mode="specific", sources=scope))
        return request, manifest

    async def dispatch(self):
        pool = await self.store.pool()
        count = await pool.fetchval("""SELECT count(*) FROM intelligence_items WHERE status='running'
            AND coalesce(result->>'runtime_status','running')<>'waiting'""")
        if hasattr(getattr(self.runs, "store", None), "engine"):
            from sqlalchemy import select, func
            table = self.runs.store.runs
            async with self.runs.store.engine.connect() as conn:
                count = await conn.scalar(select(func.count()).select_from(table).where(
                    table.c.snapshot["application"]["identity"]["kind"].as_string() == "intelligence",
                    table.c.snapshot["status"].as_string().in_(["ready", "running"])))
        capacity = max(0, settings().max_runs - count)
        rows = await pool.fetch("""SELECT i.* FROM intelligence_items i JOIN intelligence_plans p ON p.id=i.plan_id
            WHERE i.status='queued' AND i.run_id IS NULL AND (p.status='active' OR (p.status='paused' AND i.manual)) AND (i.manual OR p.mode='auto')
            AND NOT EXISTS(SELECT 1 FROM intelligence_items active WHERE active.plan_id=i.plan_id AND active.status='running')
            AND NOT EXISTS(SELECT 1 FROM intelligence_item_runs r JOIN intelligence_items prior ON prior.id=r.item_id
                WHERE prior.plan_id=i.plan_id AND (prior.id<>i.id OR r.execution_number<i.run_sequence)
                AND coalesce(r.result->>'runtime_status','unknown') NOT IN ('completed','failed','cancelled'))
            ORDER BY i.manual DESC,p.last_dispatched_at NULLS FIRST,i.create_time LIMIT $1""", capacity)
        dispatched = set()
        for raw in rows:
            item = record(raw)
            if item["plan_id"] in dispatched:
                continue
            plan = await self.store.plan(item["plan_id"])
            try:
                await principal_for_grant(item["authorization_id"], store=self.store)
                if item["run_request"]:
                    from open_deep_research.api.contracts import RunRequest
                    request, manifest = RunRequest.model_validate(item["run_request"]), item["manifest"]
                else:
                    request, manifest = await self.freeze_request(item, plan)
                from open_deep_research.api.native_runs import idempotent_run_id
                expected_run = idempotent_run_id(item["owner_id"], f"intelligence:{item['id']}:{item['run_sequence']}")
                async with pool.acquire() as c, c.transaction():
                    await budget.reserve(c, plan, f"run:{item['id']}:{item['run_sequence']}", plan["definition"]["budget"]["run_micro_usd"])
                    changed = await c.fetchval("""UPDATE intelligence_items SET run_request=$3::jsonb,manifest=$4::jsonb,
                        update_time=now() WHERE id=$1::uuid AND version=$2 AND status='queued' RETURNING id""",
                        item["id"], item["version"], encode(request.model_dump(mode="json")), encode(manifest))
                    if not changed:
                        raise IntelligenceError("work_item_changed_before_dispatch")
                    await c.execute("""INSERT INTO intelligence_item_runs(item_id,run_id,execution_number,owner_id,purpose,created_at)
                        VALUES($1::uuid,$2,$3,$4::uuid,$5,$4::uuid) ON CONFLICT(run_id) DO NOTHING""",
                        item["id"], expected_run, item["run_sequence"], item["owner_id"], "research" if item["run_sequence"] == 1 else "supplement")
                run_id = await self.runs.create_automated(request, item_id=item["id"], authorization_id=item["authorization_id"], manifest=manifest, execution_number=item["run_sequence"])
                if run_id != expected_run:
                    raise IntelligenceError("native_execution_identity_mismatch")
                async with pool.acquire() as c, c.transaction():
                    linked = await c.fetchval("""UPDATE intelligence_items SET run_id=$2,status='running',version=version+1,update_time=now()
                        WHERE id=$1::uuid AND status='queued' RETURNING id""", item["id"], run_id)
                    await c.execute("UPDATE intelligence_plans SET last_dispatched_at=now() WHERE id=$1::uuid", plan["id"])
                    if not linked:
                        await c.execute("UPDATE intelligence_items SET run_id=$2 WHERE id=$1::uuid", item["id"], run_id)
                if not linked:
                    await self.runs.cancel(run_id, item["owner_id"], command_id="intelligence-cancel-before-start:" + item["id"])
                dispatched.add(plan["id"])
            except Exception as exc:
                # Creation may have committed before a connection failed. Keep its
                # reservation and frozen request; an explicit retry uses the same key.
                # Shared work items do not expose provider diagnostics or a
                # personal profile's input values. Domain codes are public.
                await self.block(item, plan, exc.code if isinstance(exc, IntelligenceError) else type(exc).__name__)

    async def block(self, item, plan, reason):
        pool = await self.store.pool()
        async with pool.acquire() as c, c.transaction():
            await c.execute("UPDATE intelligence_items SET status='blocked',reason=$2,version=version+1,update_time=now() WHERE id=$1::uuid AND status NOT IN ('completed','cancelled','dismissed')", item["id"], reason)
            await notify(c, plan, "blocked", "blocked:" + item["id"] + ":" + digest(reason), item_id=item["id"])

    async def reconcile(self):
        pool = await self.store.pool()
        items = [record(row) for row in await pool.fetch("""SELECT * FROM intelligence_items i WHERE run_id IS NOT NULL
            AND (status='running' OR (status='cancelled' AND EXISTS(SELECT 1 FROM intelligence_budget_reservations b
              WHERE b.operation_key='run:' || i.id::text || ':' || i.run_sequence::text AND b.status IN ('reserved','unknown'))))""")]
        for item in items:
            plan = await self.store.plan(item["plan_id"])
            state, _ = await self.runs.store.load(item["run_id"], item["owner_id"])
            try:
                await principal_for_grant(item["authorization_id"], store=self.store)
            except (IntelligenceError, PermissionError) as exc:
                if state.status not in {"completed", "failed", "cancelled"}:
                    await self.runs.cancel(item["run_id"], item["owner_id"], command_id="intelligence-revoke:" + item["id"])
                    await pool.execute("UPDATE intelligence_items SET status='cancelled',reason=$2 WHERE id=$1::uuid", item["id"], str(exc))
                    continue
            if state.status not in {"completed", "failed", "cancelled"}:
                await pool.execute("UPDATE intelligence_items SET result=result || $2::jsonb WHERE id=$1::uuid", item["id"], encode({"runtime_status": state.status,
                    "pending_decision": {"stage": state.pending.stage, "question": state.pending.question} if state.pending else None}))
                if state.pending:
                    async with pool.acquire() as c, c.transaction():
                        await notify(c, plan, "research_waiting", "waiting:" + state.pending.id, item_id=item["id"])
                continue
            from open_deep_research.agentscope_runtime.report import task_evidence
            from open_deep_research.agentscope_runtime.run_config import RunConfig
            config = RunConfig.restore(state.application["configuration"]).compatibility_projection()
            quality = quality_result(state)
            from datetime import datetime, UTC
            from open_deep_research.api.projections import _stable_report_review
            gate = state.agent_states.get("supervisor", {}).get("middle_context", {}).get("quality_gate", {})
            result = {"report": state.final_report or state.report_product.get("final_report_draft", ""), "runtime_status": state.status, "completion": state.completion_outcome,
                "review_ready_at": datetime.now(UTC).isoformat(),
                "report_review": _stable_report_review(state.report_product), "evidence": task_evidence(state.findings, config),
                "quality_gate": {key: gate[key] for key in ("status", "reason_codes", "skipped_stages", "unassessed_requirement_ids") if key in gate}}
            usage = await self.runs.store.budget(item["run_id"], item["owner_id"])
            cost = settled_cost(usage)
            async with pool.acquire() as c, c.transaction():
                status = "awaiting_review" if state.status == "completed" else "cancelled" if state.status == "cancelled" else "blocked"
                await c.execute("""UPDATE intelligence_items SET status=$2,quality_status=$3,result=$4::jsonb,
                    reason=$5,version=version+1,update_time=now() WHERE id=$1::uuid AND status='running'""",
                    item["id"], status, quality, encode(result), state.error)
                await c.execute("UPDATE intelligence_item_runs SET result=$2::jsonb WHERE run_id=$1", item["run_id"], encode({**result, "quality_status": quality, "manifest": item["manifest"]}))
                if state.status == "failed" and item["status"] not in {"cancelled", "dismissed"}:
                    await budget.hold_for_retry(c, f"run:{item['id']}:{item['run_sequence']}", cost)
                else:
                    await budget.settle(c, f"run:{item['id']}:{item['run_sequence']}", cost)
                await notify(c, plan, "review_ready" if status == "awaiting_review" else "blocked",
                             f"result:{item['id']}:{item['run_sequence']}", item_id=item["id"])

    async def reconcile_budgets(self, plan_id=None):
        """Recover dispatch responses and settle only trusted native receipts."""
        pool = await self.store.pool()
        rows = [record(row) for row in await pool.fetch("""SELECT r.*,b.operation_key,b.status AS budget_status,
            i.owner_id AS current_owner,i.status AS item_status,i.run_sequence FROM intelligence_budget_reservations b
            JOIN intelligence_item_runs r ON b.operation_key='run:' || r.item_id::text || ':' || r.execution_number::text
            JOIN intelligence_items i ON i.id=r.item_id
            WHERE ($1::uuid IS NULL OR i.plan_id=$1::uuid) AND (b.status IN ('reserved','unknown') OR $1::uuid IS NOT NULL)
            ORDER BY b.create_time LIMIT 200""", plan_id)]
        reconciled = 0
        for row in rows:
            owner = row["owner_id"] or row["current_owner"]
            try:
                state, _ = await self.runs.store.load(row["run_id"], owner)
            except KeyError:
                continue  # Missing receipts never prove an external call did not execute.
            async with pool.acquire() as c, c.transaction():
                await c.execute("UPDATE intelligence_item_runs SET result=result || $2::jsonb WHERE run_id=$1", row["run_id"], encode({"runtime_status": state.status}))
                if row["execution_number"] == row["run_sequence"]:
                    await c.execute("""UPDATE intelligence_items SET run_id=$2,status=CASE WHEN status IN ('queued','blocked') THEN 'running' ELSE status END,
                        update_time=now() WHERE id=$1::uuid AND run_id IS NULL AND run_sequence=$3""", row["item_id"], row["run_id"], row["execution_number"])
            if state.status not in {"completed", "failed", "cancelled"}:
                if row["item_status"] in {"cancelled", "dismissed"}:
                    await self.runs.cancel(row["run_id"], owner, command_id="intelligence-ended:" + row["run_id"])
                continue
            usage = await self.runs.store.budget(row["run_id"], owner)
            cost = settled_cost(usage)
            async with pool.acquire() as c, c.transaction():
                retryable_item = (state.status == "failed" and row["execution_number"] == row["run_sequence"]
                    and row["item_status"] not in {"completed", "cancelled", "dismissed"})
                if retryable_item:
                    await budget.hold_for_retry(c, row["operation_key"], cost)
                elif cost is not None:
                    await budget.correct(c, row["operation_key"], cost)
                    reconciled += 1
                else:
                    await budget.settle(c, row["operation_key"], None)
                if state.final_report:
                    from open_deep_research.agentscope_runtime.report import task_evidence
                    from open_deep_research.agentscope_runtime.run_config import RunConfig
                    config = RunConfig.restore(state.application["configuration"]).compatibility_projection()
                    await c.execute("UPDATE intelligence_item_runs SET result=$2::jsonb WHERE run_id=$1", row["run_id"], encode({
                        "runtime_status": state.status, "report": state.final_report, "quality_status": quality_result(state),
                        "evidence": task_evidence(state.findings, config), "manifest": config.get("metadata", {}).get("observation_manifest", {}),
                        "cost_micro_usd": cost}))
        from open_deep_research.platform import runtime
        if runtime.enabled():
            from .service_accounting import reconcile_service_receipts
            reconciled += await reconcile_service_receipts(plan_id)
        return {"reconciled": reconciled}

    async def controls(self):
        pool = await self.store.pool()
        jobs = [record(row) for row in await pool.fetch("""UPDATE intelligence_jobs SET status='running',attempts=attempts+1,
            lease_until=now()+interval '60 seconds' WHERE id IN (SELECT id FROM intelligence_jobs WHERE kind='run_action'
            AND available_at<=now() AND (status='queued' OR (status='running' AND lease_until<now())) ORDER BY create_time LIMIT 10) RETURNING *""")]
        for job in jobs:
            item, plan = await self.store.item(job["payload"]["item_id"])
            try:
                action = job["payload"]["action"]
                if action == "cancel":
                    await self.runs.cancel(item["run_id"], item["owner_id"], command_id="control:" + job["id"])
                elif action == "feedback":
                    await self.runs.store.submit_decision(item["run_id"], item["owner_id"], "control:" + job["id"], "feedback:supervisor",
                        {"action": "feedback", "task_id": "supervisor", "feedback": job["payload"]["reason"]})
                elif action == "retry":
                    if item["status"] not in {"queued", "running", "blocked"}:
                        await pool.execute("UPDATE intelligence_jobs SET status='completed',lease_until=NULL WHERE id=$1::uuid AND attempts=$2", job["id"], job["attempts"])
                        continue
                    await principal_for_grant(item["authorization_id"], store=self.store)
                    async with pool.acquire() as c, c.transaction():
                        await budget.reopen(c, plan, f"run:{item['id']}:{item['run_sequence']}")
                    started = await self.runs.resume(item["run_id"], item["owner_id"], automation=True)
                    if started is False:
                        await pool.execute("""UPDATE intelligence_jobs SET status='queued',lease_until=NULL,available_at=now()+interval '5 seconds'
                            WHERE id=$1::uuid AND attempts=$2""", job["id"], job["attempts"])
                        continue
                    await pool.execute("UPDATE intelligence_items SET status='running' WHERE id=$1::uuid AND run_sequence=$2 AND status='queued'", item["id"], item["run_sequence"])
                await pool.execute("UPDATE intelligence_jobs SET status='completed',lease_until=NULL WHERE id=$1::uuid AND attempts=$2", job["id"], job["attempts"])
            except Exception as exc:
                await pool.execute("UPDATE intelligence_jobs SET status='failed',error=$3,lease_until=NULL WHERE id=$1::uuid AND attempts=$2", job["id"], job["attempts"], type(exc).__name__)
                await self.block(item, plan, "control_failed:" + type(exc).__name__)

    async def pass_once(self):
        await self.controls()
        if hasattr(self.runs, "recover_interrupted"):
            await self.runs.recover_interrupted(kind="intelligence")
        await self.reconcile_budgets()
        await self.reconcile()
        await self.schedule()
        await self.batch_events()
        await self.dispatch()

    async def loop(self):
        while True:
            interval = settings().poll_seconds
            try:
                from open_deep_research.platform import runtime
                if runtime.enabled():
                    current = await runtime.refresh()
                    with runtime.scope(current):
                        interval = settings().poll_seconds
                        if settings().enabled:
                            await self.pass_once()
                elif settings().enabled:
                    from .authorization import require_managed_accounting
                    require_managed_accounting()
            except Exception:
                log.exception("持续情报协调未完成，将在下一轮重试")
            await asyncio.sleep(interval)
