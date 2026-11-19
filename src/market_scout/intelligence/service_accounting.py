"""Attribute observation-to-document processing to the same plan and owner."""

import json
import asyncpg
from market_scout.documents.settings import get_document_settings
from market_scout.documents.database import get_document_pool
from .authorization import principal_for_grant, require_managed_accounting
from .contracts import IntelligenceError
from .store import Store
from . import budget


class DocumentBudgetPort:
    def __init__(self, binding):
        self.binding = binding

    async def reserve(self, identity, amount, owner):
        _, _, plan = await principal_for_grant(self.binding["authorization_id"], plan_id=self.binding["plan_id"])
        if owner != plan["owner_id"] or owner != self.binding["billing_owner_id"]:
            raise IntelligenceError("document_billing_owner_changed", 403)
        if amount == 0:
            return
        pool = await Store().pool()
        async with pool.acquire() as c, c.transaction():
            await budget.reserve(c, plan, "service:" + identity, amount)

    async def settle(self, identity, actual):
        pool = await Store().pool()
        async with pool.acquire() as c, c.transaction():
            await budget.settle(c, "service:" + identity, actual)


async def document_budget_binding(job_id):
    if not get_document_settings().configured:
        return None
    pool = await get_document_pool()
    try:
        row = await pool.fetchrow("""SELECT j.intelligence_binding,d.home_knowledge_base_id FROM research_document_jobs j
            JOIN research_documents d ON d.id=j.document_id WHERE j.id=$1::uuid""", job_id)
    except asyncpg.UndefinedColumnError:
        from .settings import settings
        if settings().enabled:
            raise IntelligenceError("intelligence_document_migration_required", 503)
        return None  # Legacy document-only test/deployment, before this optional feature.
    if not row or not row["intelligence_binding"]:
        return None
    binding = json.loads(row["intelligence_binding"]) if isinstance(row["intelligence_binding"], str) else row["intelligence_binding"]
    require_managed_accounting()
    _, _, plan = await principal_for_grant(binding["authorization_id"], plan_id=binding["plan_id"])
    if str(row["home_knowledge_base_id"]) != plan["knowledge_base_id"]:
        raise IntelligenceError("document_billing_plan_scope_mismatch", 403)
    return binding


async def reconcile_service_receipts(plan_id=None):
    from sqlalchemy import select
    from market_scout.platform import schema
    from market_scout.platform.store import get_store
    pool = await Store().pool()
    rows = await pool.fetch("""SELECT operation_key FROM intelligence_budget_reservations
        WHERE operation_key LIKE 'service:%' AND ($1::uuid IS NULL OR plan_id=$1::uuid)
        AND (status IN ('reserved','unknown') OR $1::uuid IS NOT NULL) ORDER BY update_time DESC LIMIT 200""", plan_id)
    reconciled = 0
    for row in rows:
        identity = row["operation_key"].removeprefix("service:")
        async with get_store().engine.connect() as c:
            charge = (await c.execute(select(schema.charges).where(schema.charges.c.operation_id == identity))).mappings().one_or_none()
        if charge is None or charge["status"] not in {"settled", "released"}:
            continue
        async with pool.acquire() as c, c.transaction():
            if charge["status"] == "released":
                await budget.settle(c, row["operation_key"], 0, confirmed_not_executed=True)
            else:
                await budget.correct(c, row["operation_key"], charge["actual"])
            reconciled += 1
    return reconciled
