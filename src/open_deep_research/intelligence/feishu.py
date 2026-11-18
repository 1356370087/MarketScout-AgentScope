"""Feishu identity binding and opt-in notifications; no business callbacks."""

import hashlib
import json
import secrets
import time
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, Depends
from pydantic import Field

from open_deep_research.knowledge import authz
from .contracts import Input, FeishuPreference, IntelligenceError, DeferredJob
from .settings import settings
from .store import Store, record
from .router import enabled, errors, read
from .observations import assert_job

TOKEN_URL = "https://accounts.feishu.cn/oauth/v3/token"
AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
USER_INFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"
MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
_token = None


def configured(cfg=None):
    cfg = cfg or settings()
    origin = urlsplit(cfg.public_origin)
    if not cfg.feishu_app_id or not cfg.feishu_app_secret or origin.scheme not in {"http", "https"} or not origin.hostname:
        raise IntelligenceError("feishu_not_configured", 503)
    if origin.username or origin.password or origin.query or origin.fragment or origin.path:
        raise IntelligenceError("intelligence_public_origin_invalid", 503)
    if origin.scheme != "https" and origin.hostname not in {"localhost", "127.0.0.1"}:
        raise IntelligenceError("feishu_public_origin_requires_https", 503)
    return cfg


def checked_response(response):
    response.raise_for_status()
    body = response.json()
    if body.get("code", 0) != 0 or body.get("error"):
        raise IntelligenceError("feishu_api_rejected:" + str(body.get("code", body.get("error"))))
    return body


async def tenant_token(client, cfg=None):
    global _token
    cfg = cfg or configured()
    identity = hashlib.sha256((cfg.feishu_app_id + ":" + cfg.feishu_app_secret).encode()).hexdigest()
    if _token and _token[0] == identity and _token[2] > time.monotonic():
        return _token[1]
    body = checked_response(await client.post(TOKEN_URL, data={"grant_type": "client_credentials",
        "client_id": cfg.feishu_app_id, "client_secret": cfg.feishu_app_secret, "scope": "im:message:send_as_bot"}))
    duration = int(body["expires_in"])
    _token = (identity, body["access_token"], time.monotonic() + max(0, duration - min(60, duration / 10)))
    return _token[1]


async def start_binding(actor, *, store=None):
    store = store or Store()
    cfg = configured()
    state = secrets.token_urlsafe(32)
    pool = await store.pool()
    await pool.execute("""INSERT INTO intelligence_oauth_states(user_id,app_id,state_hash,expires_at,created_at)
        VALUES($1::uuid,$3,$2,now()+interval '10 minutes',$1::uuid)""", actor, hashlib.sha256(state.encode()).hexdigest(), cfg.feishu_app_id)
    return {"url": AUTHORIZE_URL + "?" + urlencode({"client_id": cfg.feishu_app_id,
        "redirect_uri": cfg.public_origin + "/intelligence/feishu/callback", "response_type": "code", "scope": "user_profile", "state": state})}


async def finish_binding(actor, code, state, *, store=None, transport=None):
    store = store or Store()
    cfg = configured()
    pool = await store.pool()
    found = await pool.fetchval("""UPDATE intelligence_oauth_states SET consumed_at=now()
        WHERE user_id=$1::uuid AND state_hash=$2 AND app_id=$3 AND consumed_at IS NULL AND expires_at>now() RETURNING id""",
        actor, hashlib.sha256(state.encode()).hexdigest(), cfg.feishu_app_id)
    if not found:
        raise IntelligenceError("feishu_binding_state_invalid", 403)
    async with httpx.AsyncClient(timeout=30, transport=transport) as client:
        token = checked_response(await client.post(TOKEN_URL, data={"grant_type": "authorization_code",
            "client_id": cfg.feishu_app_id, "client_secret": cfg.feishu_app_secret, "code": code,
            "redirect_uri": cfg.public_origin + "/intelligence/feishu/callback"}))["access_token"]
        user = checked_response(await client.get(USER_INFO_URL, headers={"Authorization": "Bearer " + token}))["data"]
    if not user.get("open_id") or not user.get("tenant_key"):
        raise IntelligenceError("feishu_identity_incomplete")
    # No user token, refresh token, email or telephone is persisted.
    await pool.execute("""INSERT INTO intelligence_feishu_bindings(user_id,tenant_key,open_id,app_id,enabled,created_at,updated_at)
        VALUES($1::uuid,$2,$3,$4,false,$1::uuid,$1::uuid) ON CONFLICT(user_id) DO UPDATE SET
        tenant_key=EXCLUDED.tenant_key,open_id=EXCLUDED.open_id,app_id=EXCLUDED.app_id,enabled=false,update_time=now(),updated_at=EXCLUDED.updated_at""",
        actor, user["tenant_key"], user["open_id"], cfg.feishu_app_id)
    return {"bound": True, "enabled": False}


async def deliver(job, *, store=None, transport=None):
    store = store or Store()
    pool = await store.pool()
    notification = record(await pool.fetchrow("SELECT * FROM intelligence_notifications WHERE id=$1::uuid", job["payload"]["notification_id"]))
    if not notification or notification["delivery_status"] in {"sent", "dropped"}:
        return {"status": "settled"}
    if notification["delivery_status"] in {"sending", "unknown"}:
        await pool.execute("UPDATE intelligence_notifications SET delivery_status='unknown' WHERE id=$1::uuid", notification["id"])
        raise IntelligenceError("feishu_delivery_outcome_unknown")
    cfg = await live_configuration()
    if not cfg.notifications_enabled:
        raise DeferredJob()
    binding = await pool.fetchrow("SELECT * FROM intelligence_feishu_bindings WHERE user_id=$1::uuid AND app_id=$2 AND enabled", notification["recipient_id"], cfg.feishu_app_id)
    if await pool.fetchval("SELECT status FROM iam_users WHERE id=$1::uuid", notification["recipient_id"]) != "active":
        binding = None
    plan = await store.plan(notification["plan_id"])
    try:
        await store.plan(notification["plan_id"], notification["recipient_id"])
        await authz.require_kb_capability(notification["recipient_id"], plan["knowledge_base_id"], authz.CAP_VIEW)
    except (authz.AuthorizationError, IntelligenceError):
        binding = None
    if binding is None:
        await pool.execute("UPDATE intelligence_notifications SET delivery_status='dropped' WHERE id=$1::uuid", notification["id"])
        return {"status": "dropped"}
    cfg = configured(cfg)
    target = "/intelligence/items/" + notification["item_id"] if notification["item_id"] else "/intelligence/plans/" + plan["id"]
    status = {"review_ready": "研究成果待审阅", "research_proposed": "新的研究建议", "blocked": "研究需要处理",
        "authorization_required": "需要确认授权", "source_or_job_failed": "来源或任务异常", "research_waiting": "研究等待确认"}.get(notification["kind"], "计划状态已更新")
    count = notification["payload"].get("count", 1)
    card = {"header": {"title": {"tag": "plain_text", "content": "MarketScout · " + status}},
        "elements": [{"tag": "div", "text": {"tag": "plain_text", "content": f"共 {count} 项更新。请登录 MarketScout 查看并处理。"}},
            {"tag": "action", "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "在 MarketScout 查看"},
                                            "url": cfg.public_origin + target}]}]}
    async with httpx.AsyncClient(timeout=30, transport=transport) as client:
        token = await tenant_token(client, cfg)
        current = await live_configuration()
        if current.feishu_app_id != cfg.feishu_app_id or not current.notifications_enabled:
            raise DeferredJob()
        binding = await pool.fetchrow("SELECT * FROM intelligence_feishu_bindings WHERE user_id=$1::uuid AND app_id=$2 AND enabled", notification["recipient_id"], cfg.feishu_app_id)
        if not binding:
            await pool.execute("UPDATE intelligence_notifications SET delivery_status='dropped' WHERE id=$1::uuid", notification["id"])
            return {"status": "dropped"}
        if await pool.fetchval("SELECT status FROM iam_users WHERE id=$1::uuid", notification["recipient_id"]) != "active":
            await pool.execute("UPDATE intelligence_notifications SET delivery_status='dropped' WHERE id=$1::uuid", notification["id"])
            return {"status": "dropped"}
        await store.plan(notification["plan_id"], notification["recipient_id"])
        await authz.require_kb_capability(notification["recipient_id"], plan["knowledge_base_id"], authz.CAP_VIEW)
        async with pool.acquire() as c, c.transaction():
            await assert_job(c, job)
            taken = await c.fetchval("UPDATE intelligence_notifications SET delivery_status='sending' WHERE id=$1::uuid AND delivery_status='pending' RETURNING id", notification["id"])
            if not taken:
                raise IntelligenceError("feishu_delivery_already_claimed")
        try:
            response = await client.post(MESSAGE_URL, params={"receive_id_type": "open_id"},
                headers={"Authorization": "Bearer " + token}, json={"receive_id": binding["open_id"],
                "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False), "uuid": notification["id"]})
            if response.status_code == 429:
                await pool.execute("UPDATE intelligence_notifications SET delivery_status='pending' WHERE id=$1::uuid", notification["id"])
                raise IntelligenceError("feishu_rate_limited:" + response.headers.get("retry-after", "60"), 429)
            body = checked_response(response)
        except IntelligenceError as exc:
            if not exc.code.startswith("feishu_rate_limited"):
                await pool.execute("UPDATE intelligence_notifications SET delivery_status='failed' WHERE id=$1::uuid", notification["id"])
            raise
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {400, 401, 403, 404, 413, 422}:
                await pool.execute("UPDATE intelligence_notifications SET delivery_status='failed' WHERE id=$1::uuid", notification["id"])
                raise IntelligenceError("feishu_api_rejected:" + str(exc.response.status_code)) from None
            await pool.execute("UPDATE intelligence_notifications SET delivery_status='unknown' WHERE id=$1::uuid", notification["id"])
            raise IntelligenceError("feishu_delivery_outcome_unknown") from None
        except httpx.TransportError:
            await pool.execute("UPDATE intelligence_notifications SET delivery_status='unknown' WHERE id=$1::uuid", notification["id"])
            raise IntelligenceError("feishu_delivery_outcome_unknown") from None
        await pool.execute("UPDATE intelligence_notifications SET delivery_status='sent',message_id=$2,update_time=now() WHERE id=$1::uuid",
            notification["id"], body["data"]["message_id"])
    return {"status": "sent"}


router = APIRouter(prefix="/integrations/feishu", tags=["integrations"], dependencies=[Depends(enabled)])


async def live_configuration():
    """Delivery controls/identity use today's app configuration, not a job's old one."""
    from open_deep_research.platform import runtime
    if runtime.enabled():
        current = await runtime.refresh()
        secrets = await runtime.credential_values(current, None, allowed_names={"FEISHU_APP_SECRET"})
        with runtime.scope(current, secrets=secrets):
            if not settings().enabled:
                raise DeferredJob()
            return settings()
    if not settings().enabled:
        raise DeferredJob()
    return settings()


@router.get("")
async def binding_status(principal=read):
    pool = await Store().pool()
    row = await pool.fetchrow("SELECT enabled FROM intelligence_feishu_bindings WHERE user_id=$1::uuid AND app_id=$2", principal.user_id, settings().feishu_app_id)
    return {"bound": row is not None, "enabled": bool(row and row["enabled"])}


@router.post("/bind")
async def bind(principal=read):
    with errors():
        return await start_binding(principal.user_id)


class Callback(Input):
    code: str = Field(min_length=1, max_length=4096, repr=False)
    state: str = Field(min_length=1, max_length=512, repr=False)


@router.post("/callback")
async def callback(body: Callback, principal=read):
    with errors():
        return await finish_binding(principal.user_id, body.code, body.state)


@router.patch("")
async def preference(body: FeishuPreference, principal=read):
    pool = await Store().pool()
    value = await pool.fetchval("UPDATE intelligence_feishu_bindings SET enabled=$2,update_time=now() WHERE user_id=$1::uuid AND app_id=$3 RETURNING id", principal.user_id, body.enabled, settings().feishu_app_id)
    if not value:
        from fastapi import HTTPException
        raise HTTPException(409, "feishu_not_bound")
    return {"bound": True, "enabled": body.enabled}


@router.delete("")
async def unbind(principal=read):
    pool = await Store().pool()
    await pool.execute("DELETE FROM intelligence_feishu_bindings WHERE user_id=$1::uuid", principal.user_id)
    return {"bound": False, "enabled": False}
