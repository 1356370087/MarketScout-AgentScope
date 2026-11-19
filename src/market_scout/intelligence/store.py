"""PostgreSQL domain records; commands and queued effects share one transaction."""

import hashlib
import json
import logging
from datetime import date, datetime, UTC
from decimal import Decimal
from uuid import UUID

from market_scout.documents.database import get_document_pool
from market_scout.documents.identity import document_owner_id
from market_scout.knowledge import authz
from .contracts import IntelligenceError, next_check

JSON_FIELDS = {"definition", "scope", "baseline", "checkpoint", "metadata", "event_ids", "assessment",
               "manifest", "result", "review", "payload", "response", "run_request", "blocks"}


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def record(row):
    if row is None:
        return None
    result = dict(row)
    for key, value in result.items():
        if key in JSON_FIELDS and isinstance(value, str):
            result[key] = json.loads(value)
        elif isinstance(value, UUID):
            result[key] = str(value)
        elif isinstance(value, (datetime, date)):
            result[key] = value.isoformat()
        elif isinstance(value, Decimal):
            result[key] = str(value)
    return result


async def enqueue(c, plan_id, owner_id, kind, key, payload, *, delay=0):
    identity = await c.fetchval("""INSERT INTO intelligence_jobs
        (plan_id,owner_id,kind,business_key,payload,available_at,created_at)
        VALUES($1::uuid,$2::uuid,$3,$4,$5::jsonb,now()+make_interval(secs=>$6),$2::uuid)
        ON CONFLICT(business_key) DO NOTHING RETURNING id""",
        plan_id, owner_id, kind, key, encode(payload), delay)
    if identity is None:
        logging.getLogger(__name__).info("intelligence.job_duplicate_suppressed kind=%s", kind)
    return identity


async def notify(c, plan, kind, key, *, item_id=None, payload=None):
    recipients = {plan["owner_id"], *plan["definition"].get("reviewer_ids", [])}
    for recipient in recipients:
        if kind == "research_proposed":
            bucket = await c.fetchval("SELECT to_char(now() AT TIME ZONE 'UTC','YYYYMMDDHH24MI')")
            identity = f"digest:{plan['id']}:{recipient}:{bucket}"
            nid = await c.fetchval("""INSERT INTO intelligence_notifications(plan_id,recipient_id,business_key,kind,payload,created_at)
                VALUES($1::uuid,$2::uuid,$3,$4,'{"count":1}'::jsonb,$5::uuid)
                ON CONFLICT(business_key) DO UPDATE SET payload=jsonb_set(intelligence_notifications.payload,'{count}',
                    to_jsonb(coalesce((intelligence_notifications.payload->>'count')::int,0)+1)),read_at=NULL RETURNING id""",
                plan["id"], recipient, identity, kind, plan["owner_id"])
            await enqueue(c, plan["id"], plan["owner_id"], "notify", f"notify:{nid}", {"notification_id": str(nid)}, delay=60)
            continue
        nid = await c.fetchval("""INSERT INTO intelligence_notifications
            (plan_id,item_id,recipient_id,business_key,kind,payload,created_at)
            VALUES($1::uuid,$2::uuid,$3::uuid,$4,$5,$6::jsonb,$7::uuid)
            ON CONFLICT(business_key) DO NOTHING RETURNING id""",
            plan["id"], item_id, recipient, key + ":" + recipient, kind, encode(payload or {}), plan["owner_id"])
        if nid:
            await enqueue(c, plan["id"], plan["owner_id"], "notify", f"notify:{nid}", {"notification_id": str(nid)})


async def previous_command(c, actor, key, body):
    row = await c.fetchrow("SELECT * FROM intelligence_commands WHERE actor_id=$1::uuid AND command_key=$2", actor, key)
    if row:
        if row["request_hash"] != digest(body):
            raise IntelligenceError("idempotency_key_payload_mismatch")
        return record(row)["response"]


async def save_command(c, actor, key, body, response):
    await c.execute("""INSERT INTO intelligence_commands(actor_id,command_key,request_hash,response,created_at)
        VALUES($1::uuid,$2,$3,$4::jsonb,$1::uuid)""", actor, key, digest(body), encode(response))
    return response


def readable_bases(actor):
    return f"""SELECT kb.id FROM knowledge_bases kb JOIN knowledge_workspaces w ON w.id=kb.workspace_id
        WHERE kb.id IN ({authz.readable_bases_sql(actor)}) AND kb.archived_at IS NULL AND w.status='active'"""


class Store:
    def __init__(self, pool=None):
        self._pool = pool

    async def pool(self):
        return self._pool or await get_document_pool()

    async def plan(self, plan_id, actor=None, *, capability=authz.CAP_VIEW, manage=False):
        pool = await self.pool()
        row = record(await pool.fetchrow("SELECT * FROM intelligence_plans WHERE id=$1::uuid AND NOT is_deleted", plan_id))
        if not row:
            raise IntelligenceError("intelligence_plan_not_found", 404)
        if actor:
            if not await pool.fetchval("""SELECT EXISTS(SELECT 1 FROM knowledge_bases kb JOIN knowledge_workspaces w ON w.id=kb.workspace_id
                WHERE kb.id=$1::uuid AND kb.archived_at IS NULL AND w.status='active')""", row["knowledge_base_id"]):
                raise IntelligenceError("intelligence_workspace_unavailable", 403)
            await authz.require_kb_capability(actor, row["knowledge_base_id"], capability)
            if manage and row["owner_id"] != document_owner_id(actor):
                await authz.require_kb_capability(actor, row["knowledge_base_id"], authz.CAP_MANAGE)
        return row

    async def item(self, item_id, actor=None, *, manage=False):
        pool = await self.pool()
        item = record(await pool.fetchrow("SELECT * FROM intelligence_items WHERE id=$1::uuid", item_id))
        if not item:
            raise IntelligenceError("intelligence_item_not_found", 404)
        plan = await self.plan(item["plan_id"], actor, capability=authz.CAP_SUBMIT if manage else authz.CAP_VIEW, manage=manage)
        return item, plan

    async def validate_definition(self, actor, definition):
        from market_scout.platform.importing import assert_secret_free
        assert_secret_free(definition)
        context = await authz.require_kb_capability(actor, definition["knowledge_base_id"], authz.CAP_SUBMIT)
        if str(context["knowledge_base"].get("workspace_id")) != definition["workspace_id"]:
            raise IntelligenceError("knowledge_base_outside_workspace", 422)
        pool = await self.pool()
        if context["knowledge_base"].get("archived_at") or not await pool.fetchval("SELECT status='active' FROM knowledge_workspaces WHERE id=$1::uuid", definition["workspace_id"]):
            raise IntelligenceError("plan_workspace_unavailable", 422)
        for source in definition["sources"]:
            if source["kind"] == "knowledge":
                found = await pool.fetchval("""SELECT EXISTS(SELECT 1 FROM research_documents
                    WHERE id=$1::uuid AND home_knowledge_base_id=$2::uuid AND deleted_at IS NULL
                    AND current_generation_id IS NOT NULL)""", source["document_id"], definition["knowledge_base_id"])
                if not found:
                    raise IntelligenceError("knowledge_source_not_published_or_wrong_scope", 422)
        for user in definition.get("reviewer_ids", []):
            await authz.require_kb_capability(user, definition["knowledge_base_id"], authz.CAP_REVIEW)

    async def create(self, actor, body):
        definition = body.model_dump(mode="json")
        await self.validate_definition(actor, definition)
        owner = document_owner_id(actor)
        pool = await self.pool()
        async with pool.acquire() as c, c.transaction():
            row = record(await c.fetchrow("""INSERT INTO intelligence_plans
                (workspace_id,knowledge_base_id,owner_id,name,objective,definition,mode,created_at,updated_at)
                VALUES($1::uuid,$2::uuid,$3::uuid,$4,$5,$6::jsonb,$7,$3::uuid,$3::uuid) RETURNING *""",
                definition["workspace_id"], definition["knowledge_base_id"], owner, body.name, body.objective,
                encode(definition), body.mode))
            await self.sync_sources(c, row, definition)
        return row

    async def sync_sources(self, c, plan, definition):
        identities = [digest({k: source.get(k) for k in ("kind", "url", "document_id")}) for source in definition["sources"]]
        await c.execute("UPDATE intelligence_sources SET is_deleted=true,next_run_at=NULL WHERE plan_id=$1::uuid AND NOT(identity=ANY($2::text[]))", plan["id"], identities)
        for source, identity in zip(definition["sources"], identities):
            await c.execute("""INSERT INTO intelligence_sources(plan_id,identity,definition,created_at)
                VALUES($1::uuid,$2,$3::jsonb,$4::uuid) ON CONFLICT(plan_id,identity)
                DO UPDATE SET definition=EXCLUDED.definition,is_deleted=false,update_time=now()""",
                plan["id"], identity, encode(source), plan["owner_id"])

    async def list_plans(self, actor, workspace_id=None):
        pool = await self.pool()
        return [record(row) for row in await pool.fetch(f"""SELECT p.* FROM intelligence_plans p
            WHERE NOT p.is_deleted AND p.knowledge_base_id IN ({readable_bases('$1')})
            AND ($2::uuid IS NULL OR p.workspace_id=$2::uuid) ORDER BY p.update_time DESC LIMIT 200""",
            document_owner_id(actor), workspace_id)]

    async def update(self, actor, plan_id, command):
        old = await self.plan(plan_id, actor, capability=authz.CAP_SUBMIT, manage=True)
        definition = command.plan.model_dump(mode="json")
        if any(definition[k] != old[k] for k in ("workspace_id", "knowledge_base_id")):
            raise IntelligenceError("plan_scope_is_immutable")
        await self.validate_definition(actor, definition)
        pool = await self.pool()
        key, body = f"plan:{plan_id}:{command.command_id}", command.model_dump(mode="json")
        async with pool.acquire() as c, c.transaction():
            prior = await previous_command(c, actor, key, body)
            if prior is not None:
                return prior
            row = record(await c.fetchrow("""UPDATE intelligence_plans SET definition=$3::jsonb,name=$4,objective=$5,
                status='paused',mode=$6,authorization_id=NULL,version=version+1,updated_at=$7::uuid,update_time=now()
                WHERE id=$1::uuid AND version=$2 RETURNING *""", plan_id, command.expected_version,
                encode(definition), definition["name"], definition["objective"], definition["mode"], actor))
            if row is None:
                raise IntelligenceError("version_conflict", current_version=old["version"])
            await c.execute("UPDATE intelligence_authorizations SET revoked_at=now(),revoked_by=$2::uuid,reason='plan_changed' WHERE plan_id=$1::uuid AND revoked_at IS NULL", plan_id, actor)
            await self.sync_sources(c, row, definition)
            return await save_command(c, actor, key, body, row)

    async def activate(self, principal, plan_id, command):
        actor = principal.user_id
        plan = await self.plan(plan_id, actor, capability=authz.CAP_SUBMIT, manage=True)
        if plan["owner_id"] != actor:
            raise IntelligenceError("billing_owner_must_authorize", 403)
        await self.validate_definition(actor, plan["definition"])
        from .authorization import validate_runtime_configuration
        await validate_runtime_configuration(principal, plan["definition"])
        pool = await self.pool()
        key, body = f"activate:{plan_id}:{command.command_id}", command.model_dump(mode="json")
        async with pool.acquire() as c, c.transaction():
            prior = await previous_command(c, actor, key, body)
            if prior is not None:
                return prior
            row = record(await c.fetchrow("""UPDATE intelligence_plans SET status='active',mode=$3,version=version+1,
                updated_at=$4::uuid,update_time=now(),reason=NULL WHERE id=$1::uuid AND version=$2 AND status<>'archived' RETURNING *""",
                plan_id, command.expected_version, command.mode, actor))
            if row is None:
                raise IntelligenceError("version_conflict", current_version=plan["version"])
            await c.execute("UPDATE intelligence_authorizations SET revoked_at=now(),revoked_by=$2::uuid,reason='superseded' WHERE plan_id=$1::uuid AND revoked_at IS NULL", plan_id, actor)
            grant = await c.fetchval("""INSERT INTO intelligence_authorizations
                (plan_id,owner_id,authz_version,plan_version,scope,expires_at,created_at)
                VALUES($1::uuid,$2::uuid,$3,$4,$5::jsonb,$6,$2::uuid) RETURNING id""",
                plan_id, actor, principal.authz_version, row["version"], encode({**row["definition"], "mode": command.mode}), command.expires_at)
            await c.execute("UPDATE intelligence_plans SET authorization_id=$2::uuid WHERE id=$1::uuid", plan_id, str(grant))
            await c.execute("UPDATE intelligence_sources SET next_run_at=now(),failures=0,error=NULL WHERE plan_id=$1::uuid AND NOT is_deleted AND definition->>'kind'<>'knowledge_events'", plan_id)
            row["authorization_id"] = str(grant)
            return await save_command(c, actor, key, body, row)

    async def control(self, actor, plan_id, command, action, *, new_owner=None):
        plan = await self.plan(plan_id, actor, capability=authz.CAP_SUBMIT, manage=True)
        if new_owner:
            await authz.require_kb_capability(new_owner, plan["knowledge_base_id"], authz.CAP_SUBMIT)
        pool = await self.pool()
        key, body = f"{action}:{plan_id}:{command.command_id}", {**command.model_dump(mode="json"), "new_owner": new_owner}
        async with pool.acquire() as c, c.transaction():
            prior = await previous_command(c, actor, key, body)
            if prior is not None:
                return prior
            row = record(await c.fetchrow("""UPDATE intelligence_plans SET status=$3,version=version+1,
                owner_id=coalesce($5::uuid,owner_id),updated_at=$4::uuid,update_time=now()
                WHERE id=$1::uuid AND version=$2 RETURNING *""", plan_id, command.expected_version,
                "archived" if action == "archive" else "paused", actor, new_owner))
            if row is None:
                raise IntelligenceError("version_conflict", current_version=plan["version"])
            if action in {"revoke", "archive", "transfer"}:
                await c.execute("UPDATE intelligence_authorizations SET revoked_at=now(),revoked_by=$2::uuid,reason=$3 WHERE plan_id=$1::uuid AND revoked_at IS NULL", plan_id, actor, action)
            if action == "transfer":
                await notify(c, row, "authorization_required", key)
            return await save_command(c, actor, key, body, row)

    async def check(self, actor, plan_id):
        plan = await self.plan(plan_id, actor, capability=authz.CAP_SUBMIT, manage=True)
        if plan["status"] != "active":
            raise IntelligenceError("plan_not_active")
        pool = await self.pool()
        await pool.execute("UPDATE intelligence_sources SET next_run_at=now(),failures=0,error=NULL WHERE plan_id=$1::uuid AND NOT is_deleted AND definition->>'kind'<>'knowledge_events'", plan_id)
        return {"status": "queued"}

    async def history(self, plan_id, actor, kind):
        await self.plan(plan_id, actor)
        table = {"events": "intelligence_events", "decisions": "intelligence_decisions",
                 "authorizations": "intelligence_authorizations", "sources": "intelligence_sources"}[kind]
        pool = await self.pool()
        return [record(row) for row in await pool.fetch(f"SELECT * FROM {table} WHERE plan_id=$1::uuid ORDER BY create_time DESC LIMIT 200", plan_id)]

    async def items(self, actor, plan_id=None, workspace_id=None):
        pool = await self.pool()
        return [record(row) for row in await pool.fetch(f"""SELECT i.id,i.plan_id,i.owner_id,i.version,i.question,i.status,
            i.quality_status,i.reason,i.run_id,i.create_time,'{{}}'::jsonb AS manifest,'{{}}'::jsonb AS review,
            jsonb_build_object('runtime_status',i.result->'runtime_status','summary',left(i.result->>'report',500)) AS result FROM intelligence_items i
            JOIN intelligence_plans p ON p.id=i.plan_id WHERE NOT p.is_deleted
            AND p.knowledge_base_id IN ({readable_bases('$1')})
            AND ($2::uuid IS NULL OR p.id=$2::uuid) AND ($3::uuid IS NULL OR p.workspace_id=$3::uuid)
            ORDER BY i.create_time DESC LIMIT 200""", document_owner_id(actor), plan_id, workspace_id)]

    async def inbox(self, actor, workspace_id=None):
        pool = await self.pool()
        return [record(row) for row in await pool.fetch(f"""SELECT n.* FROM intelligence_notifications n
            JOIN intelligence_plans p ON p.id=n.plan_id WHERE n.recipient_id=$1::uuid
            AND p.knowledge_base_id IN ({readable_bases('$1')}) AND ($2::uuid IS NULL OR p.workspace_id=$2::uuid)
            ORDER BY n.create_time DESC LIMIT 200""", document_owner_id(actor), workspace_id)]
