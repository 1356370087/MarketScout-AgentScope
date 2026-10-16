"""平台 HTTP 边界：真实 IAM、归属检查、事务化变更和脱敏输出。"""

import hmac
import os
from uuid import uuid4
from typing import Literal
from fastapi import APIRouter, Depends, Request
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy import select, update, insert, func
from sqlalchemy.ext.asyncio import AsyncSession
from security.rbac import Principal, require_active_user
from . import schema as s
from .contracts import (BootstrapRequest, ConfigurationDocument, DraftRequest, UpdateDraft, PublishRequest,
    PreviewRequest, PreferencesRequest, CredentialRequest, CredentialReplacement, ImportRequest,
    QuotaLimits, MaintenanceRequest)
from .store import get_store, PlatformError, require, check_scope, now

class PlatformRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handler(request):
            try:
                return await original(request)
            except PlatformError as exc:
                return JSONResponse(status_code=exc.status, content={"detail": exc.detail})
            except (ValidationError, RequestValidationError) as exc:
                errors = [{"loc": list(item["loc"]), "msg": item["msg"], "type": item["type"]} for item in exc.errors()]
                return JSONResponse(status_code=422, content={"detail": errors})
            except ValueError as exc:
                return JSONResponse(status_code=422, content={"detail": str(exc)[:500]})
            except IntegrityError:
                return JSONResponse(status_code=409, content={"detail": "platform_write_conflict"})
            except OperationalError:
                return JSONResponse(status_code=503, content={"detail": "platform_database_unavailable"})
        return handler


router = APIRouter(prefix="/platform", tags=["platform"], route_class=PlatformRoute)
setup_router = APIRouter(prefix="/setup", tags=["setup"], route_class=PlatformRoute)


async def real_user(user: Principal = Depends(require_active_user)):
    if not user.session_id:
        raise PlatformError("platform_requires_authenticated_account", 403)
    return user


@setup_router.get("/status")
async def setup_status():
    state = await get_store().state()
    return {"initialized": state["initialized"], "configured": bool(state["active_revision"]),
            "setup_available": bool(os.getenv("PLATFORM_SETUP_TOKEN")) and not state["initialized"]}


@setup_router.post("/bootstrap", status_code=201)
async def bootstrap(body: BootstrapRequest, request: Request):
    from open_deep_research.api_governance import FixedWindowRateLimiter
    limiter = getattr(request.app.state, "platform_setup_limiter", None)
    if limiter is None:
        limiter = request.app.state.platform_setup_limiter = FixedWindowRateLimiter()
    allowed, _ = limiter.allow("setup:" + (request.client.host if request.client else "unknown"), 5)
    if not allowed:
        raise PlatformError("setup_rate_limited", 429)
    expected = os.getenv("PLATFORM_SETUP_TOKEN", "")
    if not expected or not hmac.compare_digest(expected, body.token):
        raise PlatformError("invalid_setup_token", 403)
    store = get_store()
    from security.rbac.services.bootstrap import bootstrap_admin
    async with store.engine.begin() as conn:
        changed = await conn.execute(update(s.state).where(s.state.c.id == 1, s.state.c.initialized.is_(False)).values(initialized=True))
        if changed.rowcount != 1:
            raise PlatformError("setup_already_completed", 409)
        async with AsyncSession(bind=conn, expire_on_commit=False) as session:
            user = await bootstrap_admin(session, email=body.email, password=body.password, display_name=body.display_name)
            if user is None:
                raise PlatformError("administrator_already_exists", 409)
            await session.flush()
            await store.audit(conn, str(user.id), "platform.initialized", str(user.id))
    return {"initialized": True}


@router.get("/catalog")
async def catalog(user=Depends(real_user)):
    from .catalog import runtime_catalog, environment_catalog
    if not any(key in user.permissions for key in ("platform.config.read", "platform.config.personal")):
        raise PlatformError("insufficient_permissions", 403)
    from open_deep_research.configuration import Configuration
    state = await get_store().state()
    active = await get_store().active() if state["active_revision"] else None
    return {"runtime": runtime_catalog(), "services": environment_catalog(),
            "defaults": {**Configuration().model_dump(mode="json"), **(active["document"]["runtime"] if active else {})},
            "policy": active["document"].get("policy", {}) if active else {}, "schema": Configuration.model_json_schema()}


@router.get("/status")
async def status(user=Depends(real_user)):
    return await get_store().state()


@router.get("/configurations")
async def configurations(user=Depends(real_user)):
    return {"items": await get_store().list_revisions(user)}


@router.post("/configurations", status_code=201)
async def create_configuration(body: DraftRequest, user=Depends(real_user)):
    return await get_store().create_draft(user, body)


@router.get("/configurations/{identity}")
async def configuration(identity: str, user=Depends(real_user)):
    row = await get_store().revision(identity, user)
    if row["scope"] == "global":
        require(user, "platform.config.read")
    return row


@router.put("/configurations/{identity}")
async def update_configuration(identity: str, body: UpdateDraft, user=Depends(real_user)):
    return await get_store().update_draft(user, identity, body)


@router.post("/configurations/{identity}/validate")
async def validate_configuration(identity: str, user=Depends(real_user)):
    row = await get_store().revision(identity, user)
    check_scope(user, row["scope"])
    from .catalog import validate_document
    from .importing import resolve_references
    validate_document(ConfigurationDocument.model_validate(resolve_references(row["document"], strict=True)), publishing=row["scope"] == "global")
    return {"valid": True, "fingerprint": row["fingerprint"]}


@router.get("/configurations/{identity}/diff")
async def configuration_diff(identity: str, user=Depends(real_user)):
    store = get_store()
    row = await store.revision(identity, user)
    check_scope(user, row["scope"])
    state = await store.state()
    previous = (await store.active())["document"] if state["active_revision"] else {}
    def flatten(value, prefix=""):
        result = {}
        for key, item in value.items():
            path = prefix + key
            if isinstance(item, dict):
                result.update(flatten(item, path + "."))
            else:
                result[path] = item
        return result
    before, after = flatten(previous), flatten(row["document"])
    return {"base_revision": state["active_revision"], "changes": [
        {"field": key, "before": before.get(key), "after": after.get(key)}
        for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)]}


@router.post("/configurations/{identity}/publish")
async def publish_configuration(identity: str, body: PublishRequest, user=Depends(real_user)):
    store = get_store()
    row = await store.publish(user, identity, body)
    from .components import apply_revision
    applications = await apply_revision(store, row)
    if all(item["status"] == "applied" for item in applications):
        row = await store.activate(user, identity)
    return {**row, "applications": applications}


@router.post("/configurations/{identity}/check-application")
async def check_application(identity: str, user=Depends(real_user)):
    store = get_store()
    row = await store.revision(identity, user)
    require(user, "platform.config.publish" if row["scope"] == "global" else "platform.config.personal")
    if row["status"] not in {"pending", "active"}:
        raise PlatformError("configuration_not_published", 409)
    from .components import apply_revision
    applications = await apply_revision(store, row)
    if all(item["status"] == "applied" for item in applications):
        row = await store.activate(user, identity)
    return {**row, "applications": applications}


@router.get("/configurations/{identity}/applications")
async def configuration_applications(identity: str, user=Depends(real_user)):
    store = get_store()
    await store.revision(identity, user)
    async with store.engine.connect() as conn:
        return {"items": [dict(row) for row in (await conn.execute(select(s.applications).where(s.applications.c.revision_id == identity))).mappings()]}


@router.post("/configurations/{identity}/copy", status_code=201)
async def copy_configuration(identity: str, user=Depends(real_user)):
    store = get_store()
    row = await store.revision(identity, user)
    scope: Literal["global", "personal"] = "global" if "platform.config.write" in user.permissions else "personal"
    document = ConfigurationDocument.model_validate(row["document"])
    if scope == "personal":
        document = ConfigurationDocument(runtime={k: v for k, v in document.runtime.items() if document.policy.get(k) and document.policy[k].editable})
    return await store.create_draft(user, DraftRequest(name=row["name"] + " · 副本", scope=scope,
        base_revision=identity, document=document))


@router.post("/configurations/{identity}/rollback")
async def rollback_configuration(identity: str, body: PublishRequest, user=Depends(real_user)):
    require(user, "platform.config.publish")
    store = get_store()
    row = await store.revision(identity, user)
    if row["scope"] != "global" or row["status"] == "draft":
        raise PlatformError("configuration_not_published", 409)
    draft = await store.create_draft(user, DraftRequest(name=row["name"] + " · 回滚", scope="global",
        base_revision=identity, document=ConfigurationDocument.model_validate(row["document"])))
    return await publish_configuration(draft["id"], body.model_copy(update={"expected_version": draft["version"]}), user)


@router.get("/configurations/{identity}/export")
async def export_configuration(identity: str, user=Depends(real_user)):
    row = await get_store().revision(identity, user)
    require(user, "platform.config.read" if row["scope"] == "global" else "platform.config.personal")
    return {"name": row["name"], "document": row["document"], "fingerprint": row["fingerprint"]}


def _import(body, user):
    check_scope(user, body.scope)
    from .importing import parse_import, server_import
    if body.format == "server":
        require(user, "platform.config.write")
        return server_import()
    return parse_import(body.format, body.content)


@router.post("/import-preview")
async def import_preview(body: ImportRequest, user=Depends(real_user)):
    document, secrets, report = _import(body, user)
    return {"document": document, "report": report}


@router.post("/import", status_code=201)
async def import_configuration(body: ImportRequest, user=Depends(real_user)):
    from .importing import replace_pending
    from .catalog import managed_runtime_keys
    document, secrets, report = _import(body, user)
    store = get_store()
    async with store.engine.begin() as conn:
        references = {}
        for name, secret in secrets.items():
            provider = name.split("_")[0].lower() if "." not in name else "openai"
            saved = await store.create_credential(user, CredentialRequest(name=name, provider=provider,
                scope=body.scope, secret=secret), conn=conn)
            references[name] = saved["id"]
        document = replace_pending(document, references)
        if body.scope == "personal":
            baseline = (await store.active(conn))["document"]
            document["runtime"] = {k: v for k, v in document["runtime"].items() if baseline.get("policy", {}).get(k, {}).get("editable")}
            document.update(services={}, sandbox=None, policy={}, quotas={}, memory={})
            document["litellm"] = {"model_list": document.get("litellm", {}).get("model_list", [])}
            global_names = {entry["model_name"] for entry in baseline.get("litellm", {}).get("model_list", [])}
            aliases = {entry["model_name"]: f"personal-{user.user_id[:8]}-{entry['model_name']}" for entry in document["litellm"]["model_list"] if entry["model_name"] in global_names}
            for entry in document["litellm"]["model_list"]:
                entry["model_name"] = aliases.get(entry["model_name"], entry["model_name"])
            for key, value in list(document["runtime"].items()):
                if isinstance(value, str):
                    document["runtime"][key] = aliases.get(value, value)
        else:
            from open_deep_research.api.configuration_routes import FRONTEND_EDITABLE_CONFIG_KEYS
            if body.format != "json":
                document["policy"] = {key: {"editable": True} for key in FRONTEND_EDITABLE_CONFIG_KEYS if key in managed_runtime_keys()}
            budget = document["services"].get("LITELLM_RUN_BUDGET_MAX_MICRO_USD")
            if budget and document["quotas"].get("max_run_cost_micro_usd") is None:
                document["quotas"]["max_run_cost_micro_usd"] = int(budget)
        draft = await store.create_draft(user, DraftRequest(name=body.name, scope=body.scope,
            document=ConfigurationDocument.model_validate(document)), conn=conn)
    return {**draft, "import_report": report}


@router.post("/configuration-preview")
async def preview_configuration(body: PreviewRequest, user=Depends(real_user)):
    from .resolution import resolve
    from open_deep_research.api.configuration_routes import FRONTEND_EDITABLE_CONFIG_KEYS
    result = await resolve(user, body)
    return {key: value for key, value in result.items() if key != "snapshot" and key != "configurable"} | {
        "configurable": {k: v for k, v in result["configurable"].items() if k in FRONTEND_EDITABLE_CONFIG_KEYS}}


@router.get("/preferences")
async def preferences(user=Depends(real_user)):
    return await get_store().preferences(user.user_id)


@router.put("/preferences")
async def save_preferences(body: PreferencesRequest, user=Depends(real_user)):
    from .resolution import resolve
    from open_deep_research.report.models import PublisherTheme
    await resolve(user, PreviewRequest(profile_revision_id=body.profile_revision_id, configurable=body.configurable))
    theme = PublisherTheme.model_validate(body.publication_theme).model_dump(mode="json")
    store = get_store()
    async with store.engine.begin() as conn:
        values = {"owner_id": user.user_id, "profile_revision_id": body.profile_revision_id,
                  "configurable": body.configurable, "publication_theme": theme}
        await conn.execute(store.upsert(s.preferences).values(**values).on_conflict_do_update(index_elements=[s.preferences.c.owner_id], set_=values))
    return values


@router.get("/credentials")
async def credentials(user=Depends(real_user)):
    from sqlalchemy import or_
    store = get_store()
    if not {"platform.credentials.manage", "platform.credentials.personal"} & user.permissions:
        raise PlatformError("insufficient_permissions", 403)
    async with store.engine.connect() as conn:
        query = select(s.credentials).where(or_(s.credentials.c.owner_id == user.user_id,
            s.credentials.c.owner_id.is_(None)))
        return {"items": [store.public_credential(row) for row in (await conn.execute(query)).mappings()]}


@router.post("/credentials", status_code=201)
async def create_credential(body: CredentialRequest, user=Depends(real_user)):
    return await get_store().create_credential(user, body)


async def _owned_credential(identity, user, conn, store):
    row = await store.credential(identity, conn=conn)
    check_scope(user, "personal" if row["owner_id"] else "global", credentials=True)
    if row["owner_id"] and row["owner_id"] != user.user_id:
        raise PlatformError("credential_not_found", 404)
    return row


@router.put("/credentials/{identity}")
async def replace_credential(identity: str, body: CredentialReplacement, user=Depends(real_user)):
    store = get_store()
    async with store.engine.begin() as conn:
        row = await _owned_credential(identity, user, conn, store)
        version = body.expected_version + 1
        cipher = store.cipher
        payload = cipher.encrypt(f"{identity}:{version}:{row['owner_id'] or 'global'}", body.secret)
        changed = await conn.execute(update(s.credentials).where(s.credentials.c.id == identity,
            s.credentials.c.version == body.expected_version).values(version=version, ciphertext=payload,
                key_id=cipher.key_id, updated_at=now()))
        if changed.rowcount != 1:
            raise PlatformError("credential_version_conflict", 409)
        await conn.execute(insert(s.credential_versions).values(credential_id=identity, version=version,
            ciphertext=payload, key_id=cipher.key_id, created_at=now()))
        await store.audit(conn, user.user_id, "credential.replaced", identity, {"version": version})
        return store.public_credential(await store.credential(identity, conn=conn))


@router.post("/credentials/{identity}/disable")
async def disable_credential(identity: str, user=Depends(real_user)):
    store = get_store()
    async with store.engine.begin() as conn:
        await _owned_credential(identity, user, conn, store)
        await conn.execute(update(s.credentials).where(s.credentials.c.id == identity).values(disabled=True, updated_at=now()))
        await store.audit(conn, user.user_id, "credential.disabled", identity)
    return {"disabled": True}


@router.get("/quotas/{owner_id}")
async def user_quotas(owner_id: str, user=Depends(real_user)):
    require(user, "platform.quotas.write")
    store = get_store()
    async with store.engine.connect() as conn:
        override = (await conn.execute(select(s.quotas).where(s.quotas.c.owner_id == owner_id))).mappings().one_or_none()
    return {"effective": await store.limits(owner_id), "override": dict(override) if override else None}


@router.put("/quotas/{owner_id}")
async def set_quotas(owner_id: str, body: QuotaLimits, user=Depends(real_user)):
    require(user, "platform.quotas.write")
    store = get_store()
    async with store.engine.begin() as conn:
        from security.rbac.models import User
        if not await conn.scalar(select(User.id).where(User.id == owner_id)):
            raise PlatformError("user_not_found", 404)
        limits = body.model_dump()
        await conn.execute(store.upsert(s.quotas).values(owner_id=owner_id, limits=limits, version=1)
            .on_conflict_do_update(index_elements=[s.quotas.c.owner_id], set_={"limits": limits, "version": s.quotas.c.version + 1}))
        await store.audit(conn, user.user_id, "quota.updated", owner_id, {"limits": limits})
    return await user_quotas(owner_id, user)


@router.get("/audit")
async def audit(limit: int = 100, user=Depends(real_user)):
    require(user, "platform.audit.read")
    async with get_store().engine.connect() as conn:
        rows = (await conn.execute(select(s.audit).order_by(s.audit.c.created_at.desc()).limit(min(max(1, limit), 500)))).mappings()
        return {"items": [dict(row) for row in rows]}


@router.get("/overview")
async def overview(request: Request, user=Depends(real_user)):
    require(user, "platform.operations.read")
    from .operations import overview as load
    return await load(request.app)


@router.get("/operations")
async def operations(request: Request, user=Depends(real_user)):
    require(user, "platform.operations.read")
    from .operations import list_operations
    return await list_operations(request.app)


@router.post("/operations")
async def maintenance(body: MaintenanceRequest, request: Request, user=Depends(real_user)):
    require(user, "platform.operations.write")
    from .operations import maintain
    return await maintain(request.app, user, body)


@router.get("/usage")
async def usage(user=Depends(real_user)):
    require(user, "platform.usage.read")
    async with get_store().engine.connect() as conn:
        rows = (await conn.execute(select(s.months).order_by(s.months.c.period.desc(), s.months.c.owner_id).limit(500))).mappings()
        pending = await conn.scalar(select(func.count()).select_from(s.charges).where(s.charges.c.status == "unknown"))
        return {"items": [dict(row) for row in rows], "unknown_operations": pending, "currency": "USD", "unit": "micro_usd", "timezone": "Asia/Shanghai"}


@router.post("/usage/reconcile")
async def reconcile_usage(user=Depends(real_user)):
    require(user, "platform.quotas.write")
    from .reconciliation import reconcile_service_charges
    result = await reconcile_service_charges()
    store = get_store()
    async with store.engine.begin() as conn:
        await store.audit(conn, user.user_id, "usage.reconciled", detail=result)
    return result
