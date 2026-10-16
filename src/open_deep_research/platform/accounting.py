"""研究之外的物理模型调用也经过同一月度配额入口。"""

from dataclasses import dataclass
import json
import math
from uuid import uuid4
from contextvars import ContextVar
from . import quota, runtime
from .store import PlatformError

current_charge = ContextVar("platform_physical_charge", default=None)


def transport_options():
    identity = current_charge.get()
    if identity is None:
        return {}
    return {"extra_headers": {"x-litellm-request-id": identity},
            "extra_body": {"metadata": {"tags": ["operation:" + identity]}}}


@dataclass
class ServiceCharge:
    identity: str
    pricing: tuple[float, float]


class UncertainServiceCall(RuntimeError):
    pass


def model_price(model, snapshot):
    from open_deep_research.models.catalog import parse_model_info
    from .components import model_aliases
    document = snapshot.get("document", snapshot)
    aliases = model_aliases(document)
    logical = next((key for key, value in aliases.items() if value == model), model)
    catalog = parse_model_info(document.get("litellm", {}))
    entry = catalog.get(logical)
    if entry:
        return entry.input_cost_per_token * 1_000_000, entry.output_cost_per_token * 1_000_000
    costs = document.get("runtime", {}).get("model_costs_per_million", {})
    prices = next((costs[key] for key in sorted(costs, key=len, reverse=True)
                   if model.startswith(key) or model.split(":", 1)[-1].startswith(key)), None)
    if prices:
        return float(prices["input"]), float(prices["output"])
    raise PlatformError("model_price_missing:" + model)


async def begin(model, payload, output_tokens, source, *, identity=None, owner=None, pricing=None):
    if not runtime.enabled():
        return None
    snapshot = runtime.snapshot() or await runtime.refresh()
    owner = runtime.billing_owner() or owner or runtime.owner() or "platform"
    if owner != "platform":
        from sqlalchemy import select
        from security.rbac.models import User
        from .store import get_store
        async with get_store().engine.connect() as conn:
            status = await conn.scalar(select(User.status).where(User.id == owner))
        if status != "active":
            raise PlatformError("billing_owner_not_active", 403)
    from .integration import check_credentials
    await check_credentials(snapshot, owner)
    price = pricing or model_price(model, snapshot)
    input_tokens = len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
    amount = math.ceil(input_tokens * price[0] + output_tokens * price[1])
    identity = identity or "service:" + str(uuid4())
    await quota.reserve(owner, identity, amount, runtime.source() or source, detail={"model": model, "pricing_micro_usd": list(price)})
    return ServiceCharge(identity, price)


async def complete(charge, usage=None, error=None):
    if charge is None:
        return
    if usage is None:
        code = getattr(error, "status_code", None)
        if code in {400, 401, 403, 404, 413, 422, 429}:
            await quota.settle(charge.identity, 0)
            return
        await quota.mark_unknown(charge.identity)
        if error is not None:
            raise UncertainServiceCall("service_call_outcome_unknown") from error
        return
    cost = math.ceil(usage.get("input_tokens", 0) * charge.pricing[0] + usage.get("output_tokens", 0) * charge.pricing[1])
    await quota.settle(charge.identity, cost)


async def call(model, payload, output_tokens, source, invoke):
    charge = await begin(model, payload, output_tokens, source)
    token = current_charge.set(charge.identity if charge else None)
    try:
        response = await invoke()
    except BaseException as exc:
        await complete(charge, error=exc)
        raise
    finally:
        current_charge.reset(token)
    usage = getattr(response, "usage", None)
    measured = {"input_tokens": getattr(usage, "prompt_tokens", 0), "output_tokens": getattr(usage, "completion_tokens", 0)} if usage is not None else None
    await complete(charge, measured)
    return response
