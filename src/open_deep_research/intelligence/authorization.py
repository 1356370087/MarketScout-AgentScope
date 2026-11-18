"""Revocable automation authority, independent of browser sessions."""

from datetime import datetime, UTC

from security.rbac.database import session_scope
from security.rbac.repositories import collect_user_permissions, get_user_by_id
from security.rbac.principal import Principal
from security.rbac.permissions import RESEARCH_RUN_CREATE
from open_deep_research.knowledge import authz
from .contracts import IntelligenceError, STRICT_QUALITY
from .store import Store, record


def require_managed_accounting():
    from open_deep_research.platform import runtime
    if not runtime.enabled():
        raise IntelligenceError("continuous_research_requires_managed_accounting", 503)


async def principal_for_grant(grant_id, *, plan_id=None, store=None):
    store = store or Store()
    pool = await store.pool()
    grant = record(await pool.fetchrow("SELECT * FROM intelligence_authorizations WHERE id=$1::uuid", grant_id))
    if grant and grant["revoked_at"] and grant["reason"] in {"superseded", "plan_changed", "transfer"}:
        raise IntelligenceError("automation_authorization_superseded", 403)
    if not grant or grant["revoked_at"] or datetime.fromisoformat(grant["expires_at"]) <= datetime.now(UTC):
        raise IntelligenceError("automation_authorization_expired_or_revoked", 403)
    if plan_id is not None and grant["plan_id"] != plan_id:
        raise IntelligenceError("automation_authorization_wrong_plan", 403)
    plan = await store.plan(grant["plan_id"])
    available = await pool.fetchval("""SELECT EXISTS(SELECT 1 FROM knowledge_bases kb
        JOIN knowledge_workspaces w ON w.id=kb.workspace_id WHERE kb.id=$1::uuid AND w.id=$2::uuid
        AND kb.archived_at IS NULL AND w.status='active')""", plan["knowledge_base_id"], plan["workspace_id"])
    if not available:
        raise IntelligenceError("automation_authorization_workspace_unavailable", 403)
    if plan["status"] == "archived" or plan["owner_id"] != grant["owner_id"] or plan["authorization_id"] != grant["id"]:
        raise IntelligenceError("automation_authorization_superseded", 403)
    async with session_scope() as db:
        user = await get_user_by_id(db, grant["owner_id"])
        if not user or str(user.status) != "active" or user.authz_version != grant["authz_version"]:
            raise IntelligenceError("automation_owner_requires_reauthorization", 403)
        roles, permissions = await collect_user_permissions(db, grant["owner_id"])
        principal = Principal(str(user.id), user.email, str(user.status), None,
                              frozenset(roles), frozenset(permissions), user.authz_version)
    if not principal.has(RESEARCH_RUN_CREATE.code):
        raise IntelligenceError("automation_research_permission_revoked", 403)
    await authz.require_kb_capability(principal.user_id, plan["knowledge_base_id"], authz.CAP_SUBMIT)
    return principal, grant, plan


async def validate_runtime_configuration(principal, definition):
    from open_deep_research.platform import runtime
    from open_deep_research.configuration import Configuration
    require_managed_accounting()
    config = dict(definition["configurable"])
    config["enable_memory"] = False
    config["max_run_cost_micro_usd"] = definition["budget"]["run_micro_usd"]
    if runtime.enabled():
        from open_deep_research.platform.contracts import PreviewRequest
        from open_deep_research.platform.resolution import resolve
        resolved = await resolve(principal, PreviewRequest(profile_revision_id=definition.get("profile_revision_id"), configurable=config))
        config = resolved["configurable"]
    cfg = Configuration.from_runnable_config({"configurable": config})
    for key, required in STRICT_QUALITY.items():
        if getattr(cfg, key) is not required:
            raise IntelligenceError("continuous_research_requires_strict_quality:" + key, 422)
    if cfg.quality_evaluation_min_sources < 1:
        raise IntelligenceError("continuous_research_requires_source_evidence", 422)
    if cfg.enable_memory:
        raise IntelligenceError("continuous_research_requires_scoped_memory", 422)
    return cfg


async def authorize_run(owner, identity):
    require_managed_accounting()
    principal, _, _ = await principal_for_grant(identity["authorization_id"], plan_id=identity["plan_id"])
    if principal.user_id != owner:
        raise IntelligenceError("automation_owner_mismatch", 403)
    await check_work_item(identity)
    return principal


async def check_work_item(identity):
    pool = await Store().pool()
    item = await pool.fetchrow("""SELECT i.*,b.status AS reservation_status FROM intelligence_items i
        LEFT JOIN intelligence_budget_reservations b ON b.operation_key='run:' || i.id::text || ':' || i.run_sequence::text
        WHERE i.id=$1::uuid""", identity["item_id"])
    if (not item or str(item["plan_id"]) != identity["plan_id"] or str(item["authorization_id"]) != identity["authorization_id"]
        or item["run_sequence"] != identity["execution_number"] or item["status"] not in {"queued", "running", "blocked"}):
        raise IntelligenceError("automation_work_item_inactive", 403)
    if item["reservation_status"] not in {"reserved", "unknown"}:
        raise IntelligenceError("automation_budget_reservation_inactive", 403)


def validate_frozen_automation(config, budget_limit):
    """Platform/environment resolution cannot widen the owner's grant."""
    for key, required in STRICT_QUALITY.items():
        if config.get(key) is not required:
            raise IntelligenceError("continuous_research_requires_strict_quality:" + key, 422)
    if config.get("quality_evaluation_min_sources") < 1:
        raise IntelligenceError("continuous_research_requires_source_evidence", 422)
    if config.get("enable_memory") is not False:
        raise IntelligenceError("continuous_research_requires_scoped_memory", 422)
    maximum = config.get("max_run_cost_micro_usd")
    if maximum is None or not 0 < maximum <= budget_limit:
        raise IntelligenceError("resolved_run_budget_exceeds_authorization", 422)


async def check_run_identity(identity):
    if identity.get("kind") == "intelligence":
        require_managed_accounting()
        await principal_for_grant(identity["authorization_id"], plan_id=identity["plan_id"])
        await check_work_item(identity)


async def check_budget_override(identity, limits):
    _, grant, _ = await principal_for_grant(identity["authorization_id"], plan_id=identity["plan_id"])
    if "cost_micro_usd" in limits:
        value = limits["cost_micro_usd"]
        if value is None or not 0 < value <= grant["scope"]["budget"]["run_micro_usd"]:
            raise IntelligenceError("automation_budget_change_requires_plan_authorization", 403)
