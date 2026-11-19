"""Authenticated public commands; no endpoint accepts an automation identity."""

from contextlib import contextmanager
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field

from security.rbac.dependencies import require_permissions
from security.rbac.permissions import DOCUMENT_READ_OWN, DOCUMENT_WRITE_OWN, RESEARCH_RUN_CREATE
from market_scout.knowledge import authz
from .contracts import PlanCreate, PlanUpdate, VersionedCommand, Activate, ItemAction, ItemReview, IntelligenceError
from .settings import settings
from .store import Store, record, encode, enqueue, previous_command, save_command, readable_bases
from market_scout.documents.identity import document_owner_id


def enabled():
    if not settings().enabled:
        raise HTTPException(503, "intelligence_disabled")


@contextmanager
def errors():
    try:
        yield
    except IntelligenceError as exc:
        raise HTTPException(exc.status, {"code": exc.code, "current_version": exc.current_version}) from exc
    except authz.AuthorizationError as exc:
        raise HTTPException(403, str(exc)) from exc


router = APIRouter(prefix="/intelligence", tags=["intelligence"], dependencies=[Depends(enabled)])
read = Depends(require_permissions(DOCUMENT_READ_OWN.code))
write = Depends(require_permissions(RESEARCH_RUN_CREATE.code))
review_write = Depends(require_permissions(DOCUMENT_WRITE_OWN.code))


async def capabilities(plan, principal):
    caps = await authz.kb_capabilities(principal.user_id, plan["knowledge_base_id"])
    own = plan["owner_id"] == document_owner_id(principal.user_id)
    return {"manage": principal.has(RESEARCH_RUN_CREATE.code) and ((own and authz.CAP_SUBMIT in caps) or authz.CAP_MANAGE in caps),
            "review": authz.CAP_REVIEW in caps and principal.has(DOCUMENT_WRITE_OWN.code), "authorize": own and principal.user_id != "local-dev-user" and principal.has(RESEARCH_RUN_CREATE.code)}


def public_result(value):
    from market_scout.api.projections import _stable_report_review
    result = dict(value)
    if result.get("report_review"):
        result["report_review"] = _stable_report_review(result)
    if result.get("quality_gate"):
        result["quality_gate"] = {key: result["quality_gate"][key] for key in
            ("status", "reason_codes", "skipped_stages", "unassessed_requirement_ids") if key in result["quality_gate"]}
    return result


@router.get("/options")
async def options(principal=read):
    pool = await Store().pool()
    bases = [record(row) for row in await pool.fetch(f"""SELECT kb.id,kb.name,kb.workspace_id,w.name AS workspace_name
        FROM knowledge_bases kb JOIN knowledge_workspaces w ON w.id=kb.workspace_id
        WHERE kb.id IN ({authz.readable_bases_sql('$1')}) AND kb.archived_at IS NULL AND w.status='active' ORDER BY w.name,kb.name""", principal.user_id)]
    for base in bases:
        caps = await authz.kb_capabilities(principal.user_id, base["id"])
        base["can_submit"], base["can_review"] = authz.CAP_SUBMIT in caps, authz.CAP_REVIEW in caps
    docs = [record(row) for row in await pool.fetch("""SELECT id,filename,home_knowledge_base_id AS knowledge_base_id
        FROM research_documents WHERE home_knowledge_base_id=ANY($1::uuid[]) AND current_generation_id IS NOT NULL
        AND deleted_at IS NULL ORDER BY filename LIMIT 500""", [row["id"] for row in bases])]
    members = [record(row) for row in await pool.fetch("""SELECT kb.id AS knowledge_base_id,u.id AS user_id,
        coalesce(nullif(u.display_name,''),'成员 ' || left(u.id::text,8)) AS name,
        (wm.role IN ('owner','admin') OR bm.role='manager') AS can_review
        FROM knowledge_bases kb JOIN knowledge_workspace_members wm ON wm.workspace_id=kb.workspace_id
        JOIN iam_users u ON u.id=wm.user_id LEFT JOIN knowledge_base_members bm ON bm.knowledge_base_id=kb.id AND bm.user_id=u.id
        WHERE kb.id=ANY($1::uuid[]) AND u.status='active'
        AND (wm.role IN ('owner','admin') OR bm.role IN ('contributor','manager')) ORDER BY name""", [row["id"] for row in bases])]
    facts = [record(row) for row in await pool.fetch("""SELECT id,raw_statement AS name,knowledge_base_id
        FROM knowledge_fact_assertions WHERE knowledge_base_id=ANY($1::uuid[]) AND status='published'
        ORDER BY published_at DESC LIMIT 500""", [row["id"] for row in bases])]
    pages = [record(row) for row in await pool.fetch("""SELECT r.id,p.title AS name,p.knowledge_base_id
        FROM knowledge_pages p JOIN knowledge_page_revisions r ON r.id=p.published_revision_id
        WHERE p.knowledge_base_id=ANY($1::uuid[]) AND r.status='published' ORDER BY r.published_at DESC LIMIT 500""", [row["id"] for row in bases])]
    return {"bases": bases, "documents": docs, "members": members, "facts": facts, "wiki_revisions": pages}


@router.get("/plans")
async def plans(workspace_id: UUID | None = None, principal=read):
    with errors():
        return {"items": await Store().list_plans(principal.user_id, str(workspace_id) if workspace_id else None)}


@router.post("/plans", status_code=201)
async def create_plan(body: PlanCreate, principal=write):
    with errors():
        return await Store().create(principal.user_id, body)


@router.get("/plans/{plan_id}")
async def plan(plan_id: UUID, principal=read):
    with errors():
        store = Store()
        value = await store.plan(str(plan_id), principal.user_id)
        value["capabilities"] = await capabilities(value, principal)
        value["sources"] = await store.history(str(plan_id), principal.user_id, "sources")
        pool = await store.pool()
        value["budget_periods"] = [record(row) for row in await pool.fetch("SELECT period,reserved,settled FROM intelligence_budget_periods WHERE plan_id=$1::uuid ORDER BY period DESC LIMIT 12", str(plan_id))]
        return value


@router.patch("/plans/{plan_id}")
async def update_plan(plan_id: UUID, body: PlanUpdate, principal=write):
    with errors():
        return await Store().update(principal.user_id, str(plan_id), body)


@router.post("/plans/{plan_id}/activate")
async def activate(plan_id: UUID, body: Activate, principal=write):
    with errors():
        return await Store().activate(principal, str(plan_id), body)


@router.post("/plans/{plan_id}/pause")
async def pause(plan_id: UUID, body: VersionedCommand, principal=write):
    with errors():
        return await Store().control(principal.user_id, str(plan_id), body, "pause")


@router.post("/plans/{plan_id}/archive")
async def archive(plan_id: UUID, body: VersionedCommand, principal=write):
    with errors():
        return await Store().control(principal.user_id, str(plan_id), body, "archive")


class Transfer(VersionedCommand):
    new_owner_id: UUID


class ManualRequest(VersionedCommand):
    question: str = Field(min_length=1, max_length=8000)


@router.post("/plans/{plan_id}/requests", status_code=201)
async def manual_request(plan_id: UUID, body: ManualRequest, principal=write):
    with errors():
        store = Store()
        plan = await store.plan(str(plan_id), principal.user_id, capability=authz.CAP_SUBMIT, manage=True)
        from .authorization import principal_for_grant
        await principal_for_grant(plan["authorization_id"], store=store)
        pool = await store.pool()
        key, payload = f"manual:{plan_id}:{body.command_id}", body.model_dump(mode="json")
        async with pool.acquire() as c, c.transaction():
            prior = await previous_command(c, principal.user_id, key, payload)
            if prior is not None:
                return prior
            version = await c.fetchval("UPDATE intelligence_plans SET version=version+1 WHERE id=$1::uuid AND version=$2 RETURNING version", str(plan_id), body.expected_version)
            if version is None:
                raise IntelligenceError("version_conflict", current_version=plan["version"])
            item = record(await c.fetchrow("""INSERT INTO intelligence_items(plan_id,authorization_id,owner_id,question,status,manual,reason,created_at)
                VALUES($1::uuid,$2::uuid,$3::uuid,$4,'queued',true,'人工复核请求',$5) RETURNING *""",
                str(plan_id), plan["authorization_id"], plan["owner_id"], body.question, principal.user_id))
            return await save_command(c, principal.user_id, key, payload, item)


class BaselineUpdate(VersionedCommand):
    fact_ids: list[UUID] = Field(default_factory=list, max_length=100)
    wiki_revision_ids: list[UUID] = Field(default_factory=list, max_length=100)


@router.post("/plans/{plan_id}/baseline")
async def link_baseline(plan_id: UUID, body: BaselineUpdate, principal=write):
    with errors():
        store = Store()
        plan = await store.plan(str(plan_id), principal.user_id, capability=authz.CAP_SUBMIT, manage=True)
        pool = await store.pool()
        key, payload = f"baseline:{plan_id}:{body.command_id}", body.model_dump(mode="json")
        async with pool.acquire() as c, c.transaction():
            prior = await previous_command(c, principal.user_id, key, payload)
            if prior is not None:
                return prior
            facts = await c.fetch("SELECT id FROM knowledge_fact_assertions WHERE id=ANY($1::uuid[]) AND knowledge_base_id=$2::uuid AND status='published'", body.fact_ids, plan["knowledge_base_id"])
            pages = await c.fetch("""SELECT r.id FROM knowledge_page_revisions r JOIN knowledge_pages p ON p.id=r.page_id
                WHERE r.id=ANY($1::uuid[]) AND p.knowledge_base_id=$2::uuid AND r.status='published'""", body.wiki_revision_ids, plan["knowledge_base_id"])
            if {r["id"] for r in facts} != set(body.fact_ids) or {r["id"] for r in pages} != set(body.wiki_revision_ids):
                raise IntelligenceError("baseline_outside_published_scope", 403)
            baseline = {"fact_ids": payload["fact_ids"], "wiki_revision_ids": payload["wiki_revision_ids"], "actor_id": principal.user_id}
            version = await c.fetchval("""UPDATE intelligence_plans SET baseline=$3::jsonb,version=version+1,update_time=now(),updated_at=$4
                WHERE id=$1::uuid AND version=$2 RETURNING version""", str(plan_id), body.expected_version, encode(baseline), principal.user_id)
            if version is None:
                raise IntelligenceError("version_conflict", current_version=plan["version"])
            return await save_command(c, principal.user_id, key, payload, {"version": version, "baseline": baseline})


@router.post("/plans/{plan_id}/transfer")
async def transfer(plan_id: UUID, body: Transfer, principal=write):
    with errors():
        return await Store().control(principal.user_id, str(plan_id), body, "transfer", new_owner=str(body.new_owner_id))


@router.post("/plans/{plan_id}/authorizations")
async def authorize(plan_id: UUID, body: Activate, principal=write):
    with errors():
        return await Store().activate(principal, str(plan_id), body)


@router.post("/plans/{plan_id}/authorizations/revoke")
async def revoke(plan_id: UUID, body: VersionedCommand, principal=write):
    with errors():
        return await Store().control(principal.user_id, str(plan_id), body, "revoke")


@router.post("/plans/{plan_id}/check")
async def check(plan_id: UUID, principal=write):
    with errors():
        return await Store().check(principal.user_id, str(plan_id))


@router.post("/plans/{plan_id}/reconcile-budget")
async def reconcile_budget(plan_id: UUID, request: Request, principal=write):
    with errors():
        await Store().plan(str(plan_id), principal.user_id, capability=authz.CAP_SUBMIT, manage=True)
        from .coordinator import Coordinator
        return await Coordinator(request.app.state.native_research_service).reconcile_budgets(str(plan_id))


@router.get("/plans/{plan_id}/events")
async def events(plan_id: UUID, principal=read):
    with errors():
        return {"items": await Store().history(str(plan_id), principal.user_id, "events")}


@router.get("/plans/{plan_id}/decisions")
async def decisions(plan_id: UUID, principal=read):
    with errors():
        return {"items": await Store().history(str(plan_id), principal.user_id, "decisions")}


@router.get("/plans/{plan_id}/authorizations")
async def authorizations(plan_id: UUID, principal=read):
    with errors():
        return {"items": await Store().history(str(plan_id), principal.user_id, "authorizations")}


@router.get("/items")
async def items(plan_id: UUID | None = None, workspace_id: UUID | None = None, principal=read):
    with errors():
        rows = await Store().items(principal.user_id, str(plan_id) if plan_id else None, str(workspace_id) if workspace_id else None)
        for row in rows:
            row.pop("run_request", None)
            row["result"] = public_result(row["result"])
        return {"items": rows}


@router.get("/items/{item_id}")
async def item(item_id: UUID, principal=read):
    with errors():
        value, plan = await Store().item(str(item_id), principal.user_id)
        value["capabilities"] = await capabilities(plan, principal)
        # Execution requests may contain personal profile preferences; only
        # public progress and authorized artifacts are shared with the team.
        value.pop("run_request", None)
        value["result"] = public_result(value["result"])
        return value


@router.post("/items/{item_id}/actions")
async def item_action(item_id: UUID, body: ItemAction, principal=write):
    with errors():
        store = Store()
        value, plan = await store.item(str(item_id), principal.user_id, manage=True)
        pool = await store.pool()
        key, payload = f"item:{item_id}:{body.command_id}", body.model_dump(mode="json")
        async with pool.acquire() as c, c.transaction():
            prior = await previous_command(c, principal.user_id, key, payload)
            if prior is not None:
                return prior
            allowed = {"start": {"proposed"}, "retry": {"blocked"}, "feedback": {"running"},
                       "cancel": {"queued", "running", "blocked"}, "dismiss": {"proposed", "awaiting_review", "blocked"},
                       "supplement": {"awaiting_review", "completed", "blocked"}}
            if value["status"] not in allowed[body.action]:
                raise IntelligenceError("invalid_work_item_transition")
            if body.action == "supplement":
                from .authorization import principal_for_grant
                await principal_for_grant(plan["authorization_id"])
                if value["run_id"]:
                    await c.execute("UPDATE intelligence_item_runs SET result=result || $2::jsonb WHERE run_id=$1",
                        value["run_id"], encode({"review": value["review"]}))
                version = await c.fetchval("""UPDATE intelligence_items SET status='queued',question=$3,run_sequence=run_sequence+1,
                    authorization_id=$4::uuid,owner_id=$5::uuid,run_id=NULL,run_request=NULL,manifest='{}'::jsonb,
                    result='{}'::jsonb,review='{}'::jsonb,quality_status='unassessed',manual=true,version=version+1,
                    updated_at=$6::uuid,update_time=now() WHERE id=$1::uuid AND version=$2 RETURNING version""",
                    str(item_id), body.expected_version, body.reason, plan["authorization_id"], plan["owner_id"], principal.user_id)
                if version is None:
                    raise IntelligenceError("version_conflict", current_version=value["version"])
                return await save_command(c, principal.user_id, key, payload, {"status": "queued", "version": version})
            status = ("queued" if body.action in {"start", "retry"} else "cancelled" if body.action == "cancel" else
                      "dismissed" if body.action == "dismiss" else value["status"])
            version = await c.fetchval("""UPDATE intelligence_items SET status=$3,reason=$4,manual=true,version=version+1,
                updated_at=$5::uuid,update_time=now() WHERE id=$1::uuid AND version=$2 RETURNING version""",
                str(item_id), body.expected_version, status, body.reason, principal.user_id)
            if version is None:
                raise IntelligenceError("version_conflict", current_version=value["version"])
            if value["run_id"] and body.action in {"feedback", "cancel", "retry"}:
                await enqueue(c, plan["id"], value["owner_id"], "run_action", key,
                    {"item_id": str(item_id), "action": body.action, "reason": body.reason, "actor_id": principal.user_id})
            return await save_command(c, principal.user_id, key, payload, {"status": status, "version": version})


@router.post("/items/{item_id}/review")
async def review(item_id: UUID, body: ItemReview, principal=review_write):
    from .review import review_item
    with errors():
        return await review_item(principal.user_id, str(item_id), body)


@router.get("/snapshots/{snapshot_id}")
async def snapshot(snapshot_id: UUID, offset: int = Query(default=0, ge=0), limit: int = Query(default=4000, ge=1, le=8192), principal=read):
    from .observations import get_snapshot, read_document
    with errors():
        value = await get_snapshot(str(snapshot_id), principal.user_id)
        document = read_document(value)
        end = min(len(document.markdown), offset + limit)
        value.pop("artifact", None)
        return {**value, "content": document.markdown[offset:end], "next_offset": end if end < len(document.markdown) else None}


@router.get("/items/{item_id}/runs")
async def item_runs(item_id: UUID, principal=read):
    with errors():
        store = Store()
        await store.item(str(item_id), principal.user_id)
        pool = await store.pool()
        return {"items": [record(row) for row in await pool.fetch(
            """SELECT run_id,purpose,create_time,jsonb_build_object('runtime_status',result->'runtime_status',
                'quality_status',result->'quality_status','summary',left(result->>'report',500)) AS result
                FROM intelligence_item_runs WHERE item_id=$1::uuid ORDER BY create_time DESC LIMIT 50""", str(item_id))]}


@router.get("/items/{item_id}/runs/{run_id}")
async def item_run(item_id: UUID, run_id: str, principal=read):
    with errors():
        store = Store()
        await store.item(str(item_id), principal.user_id)
        pool = await store.pool()
        row = record(await pool.fetchrow("SELECT run_id,purpose,result,create_time FROM intelligence_item_runs WHERE item_id=$1::uuid AND run_id=$2", str(item_id), run_id))
        if not row:
            raise IntelligenceError("intelligence_run_not_found", 404)
        row["result"] = public_result(row["result"])
        return row


@router.get("/inbox")
async def inbox(workspace_id: UUID | None = None, principal=read):
    with errors():
        return {"items": await Store().inbox(principal.user_id, str(workspace_id) if workspace_id else None)}


@router.post("/inbox/{notification_id}/read")
async def mark_read(notification_id: UUID, principal=read):
    pool = await Store().pool()
    await pool.execute("UPDATE intelligence_notifications SET read_at=now() WHERE id=$1::uuid AND recipient_id=$2::uuid", str(notification_id), principal.user_id)
    return {"status": "read"}


@router.get("/health")
async def health(principal=read):
    pool = await Store().pool()
    jobs = [dict(row) for row in await pool.fetch(f"""SELECT j.status,count(*)::int AS count
        FROM intelligence_jobs j JOIN intelligence_plans p ON p.id=j.plan_id
        WHERE p.knowledge_base_id IN ({readable_bases('$1')}) GROUP BY j.status""", principal.user_id)]
    timing = await pool.fetchrow(f"""SELECT
        percentile_cont(0.95) WITHIN GROUP(ORDER BY extract(epoch FROM j.started_at-j.create_time)) AS queue_p95_seconds,
        percentile_cont(0.95) WITHIN GROUP(ORDER BY extract(epoch FROM j.completed_at-j.started_at)) AS duration_p95_seconds
        FROM intelligence_jobs j JOIN intelligence_plans p ON p.id=j.plan_id
        WHERE p.knowledge_base_id IN ({readable_bases('$1')}) AND j.create_time>now()-interval '1 day'""", principal.user_id)
    heartbeat = await pool.fetchval("SELECT completed_at FROM intelligence_jobs WHERE business_key='intelligence-worker'")
    sources = await pool.fetchval(f"""SELECT count(*) FROM intelligence_sources s JOIN intelligence_plans p ON p.id=s.plan_id
        WHERE s.failures>0 AND p.knowledge_base_id IN ({readable_bases('$1')})""", principal.user_id)
    metrics = await pool.fetchrow(f"""WITH allowed AS (SELECT id FROM intelligence_plans WHERE knowledge_base_id IN ({readable_bases('$1')}))
        SELECT (SELECT count(*) FROM intelligence_events WHERE plan_id IN (SELECT id FROM allowed) AND status IN ('pending','assessing')) AS pending_events,
        (SELECT count(*) FROM intelligence_items WHERE plan_id IN (SELECT id FROM allowed) AND status='queued') AS queued_research,
        (SELECT coalesce(sum(amount),0) FROM intelligence_budget_reservations WHERE plan_id IN (SELECT id FROM allowed) AND status='unknown') AS unresolved_reservation_micro_usd,
        (SELECT coalesce(max(greatest(0,extract(epoch FROM now()-s.next_run_at))),0) FROM intelligence_sources s JOIN intelligence_plans p ON p.id=s.plan_id
          WHERE p.id IN (SELECT id FROM allowed) AND p.status='active' AND NOT s.is_deleted) AS max_check_delay_seconds,
        (SELECT percentile_cont(0.95) WITHIN GROUP(ORDER BY extract(epoch FROM (review->>'reviewed_at')::timestamptz-(result->>'review_ready_at')::timestamptz))
          FROM intelligence_items WHERE plan_id IN (SELECT id FROM allowed) AND status IN ('completed','dismissed')
          AND review->>'reviewed_at' IS NOT NULL AND result->>'review_ready_at' IS NOT NULL) AS review_p95_seconds""", principal.user_id)
    decisions = [dict(row) for row in await pool.fetch(f"""SELECT d.assessment->>'decision' AS decision,count(*)::int AS count
        FROM intelligence_decisions d JOIN intelligence_plans p ON p.id=d.plan_id WHERE p.knowledge_base_id IN ({readable_bases('$1')}) GROUP BY 1""", principal.user_id)]
    quality = [dict(row) for row in await pool.fetch(f"""SELECT i.quality_status,count(*)::int AS count FROM intelligence_items i
        JOIN intelligence_plans p ON p.id=i.plan_id WHERE p.knowledge_base_id IN ({readable_bases('$1')}) GROUP BY 1""", principal.user_id)]
    deliveries = [dict(row) for row in await pool.fetch(f"""SELECT n.delivery_status,count(*)::int AS count FROM intelligence_notifications n
        JOIN intelligence_plans p ON p.id=n.plan_id WHERE n.recipient_id=$1::uuid AND p.knowledge_base_id IN ({readable_bases('$1')}) GROUP BY 1""", principal.user_id)]
    return {"jobs": jobs, "timing": dict(timing), "unhealthy_sources": sources,
            "worker_last_seen": heartbeat.isoformat() if heartbeat else None,
            "metrics": record(metrics), "decisions": decisions, "quality": quality, "deliveries": deliveries}
