"""受配置归属和月度配额约束的模型目录与连接测试。"""

import asyncio
import math
import os
import time
from uuid import uuid4
from typing import Literal
import httpx
from fastapi import APIRouter, Depends
from pydantic import Field

from .contracts import StrictModel, DraftRequest, ConfigurationDocument
from .router import real_user, PlatformRoute
from .store import get_store, PlatformError, check_scope
from .components import model_aliases
from . import quota

router = APIRouter(prefix="/platform/models", tags=["platform"], route_class=PlatformRoute)


class AddModel(StrictModel):
    scope: Literal["global", "personal"] = "personal"
    name: str = Field(min_length=1, max_length=160)
    model: dict


class TestModel(StrictModel):
    revision_id: str
    model_name: str


@router.get("")
async def models(user=Depends(real_user)):
    store = get_store()
    state = await store.state()
    rows = await store.list_revisions(user)
    items = []
    for summary in rows:
        if summary["status"] != "active" or (summary["scope"] == "global" and summary["id"] != state["active_revision"]):
            continue
        row = await store.revision(summary["id"], user)
        aliases = model_aliases(row["document"])
        items.extend({"name": entry["model_name"], "deployment": aliases[entry["model_name"]],
            "scope": row["scope"], "revision_id": row["id"], "model_info": entry.get("model_info", {}),
            "provider_model": entry.get("litellm_params", {}).get("model")}
            for entry in row["document"].get("litellm", {}).get("model_list", []))
    return {"items": items}


@router.post("", status_code=201)
async def add_model(body: AddModel, user=Depends(real_user)):
    check_scope(user, body.scope, credentials=True)
    store = get_store()
    if body.scope == "global":
        state = await store.state()
        if state["active_revision"]:
            active = await store.active()
            document = ConfigurationDocument.model_validate(active["document"])
        else:
            from open_deep_research.agentscope_runtime.models import ROLES
            document = ConfigurationDocument(runtime={**{field: body.model.get("model_name") for field, _, _ in ROLES.values()}, "model_backend": "litellm"})
    else:
        document = ConfigurationDocument()
    entries = document.litellm.setdefault("model_list", [])
    if any(item.get("model_name") == body.model.get("model_name") for item in entries):
        raise PlatformError("model_alias_already_exists", 409)
    entries.append(body.model)
    return await store.create_draft(user, DraftRequest(name=body.name, scope=body.scope, document=document))


@router.post("/test")
async def test_model(body: TestModel, user=Depends(real_user)):
    store = get_store()
    row = await store.revision(body.revision_id, user)
    check_scope(user, row["scope"], credentials=True)
    if row["status"] != "active":
        raise PlatformError("configuration_not_active", 409)
    entries = row["document"].get("litellm", {}).get("model_list", [])
    entry = next((item for item in entries if item["model_name"] == body.model_name), None)
    if not entry:
        raise PlatformError("model_not_found", 404)
    from .integration import check_credentials
    await check_credentials(row["document"], user.user_id)
    from open_deep_research.models.catalog import parse_model_info
    catalog = parse_model_info({"model_list": [entry]})
    if body.model_name not in catalog:
        raise PlatformError("model_price_missing")
    info = catalog[body.model_name]
    reserve = math.ceil((256 * info.input_cost_per_token + 8 * info.output_cost_per_token) * 1_000_000)
    identity = "connection-test:" + str(uuid4())
    await quota.reserve(user.user_id, identity, reserve, "connection_test")
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(os.environ["LITELLM_BASE_URL"].rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + os.environ["LITELLM_MASTER_KEY"]},
                json={"model": model_aliases(row["document"])[body.model_name], "messages": [{"role": "user", "content": "Reply OK."}],
                      "max_tokens": 8, "stream": False, "metadata": {"tags": [identity, "owner:" + user.user_id]}})
            response.raise_for_status()
            usage = response.json().get("usage")
            if usage is None:
                await quota.mark_unknown(identity)
                raise PlatformError("connection_test_usage_unknown", 503)
            amount = math.ceil((usage.get("prompt_tokens", 0) * info.input_cost_per_token + usage.get("completion_tokens", 0) * info.output_cost_per_token) * 1_000_000)
            await quota.settle(identity, amount)
    except (httpx.HTTPError, asyncio.CancelledError):
        await quota.mark_unknown(identity)
        raise PlatformError("model_connection_test_failed", 502) from None
    async with store.engine.begin() as conn:
        await store.audit(conn, user.user_id, "model.connection_test", identity, {"revision_id": row["id"], "model_name": body.model_name})
    return {"status": "ok", "duration_ms": round((time.monotonic() - started) * 1000), "cost_micro_usd": amount}
