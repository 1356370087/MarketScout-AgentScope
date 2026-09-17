"""Each native policy attempt owns one durable usage receipt (T023)."""

import json
import math
from contextlib import aclosing
from contextvars import ContextVar
from copy import deepcopy

from agentscope.model import ChatResponse, StructuredResponse

current_accounting = ContextVar("model_attempt_accounting", default=None)


class RecordedModelFailure(RuntimeError):
    """Replay a sanitized failed attempt without repeating the provider call."""

    def __init__(self, receipt):
        super().__init__(receipt["error_type"])
        self.status_code = receipt.get("status_code")
        self.code = receipt.get("code")
        self.uncertain = receipt.get("uncertain", False)


def response_usage(response):
    raw = (getattr(response, "metadata", None) or {}).get("raw_usage")
    if isinstance(raw, dict):
        return {name: raw[name] for name in ("input_tokens", "output_tokens") if raw.get(name) is not None} or None
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    return {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}


class AttemptAccounting:
    """Scoped to a logical model operation; candidate/continuation calls share IDs."""

    def __init__(self, session, key, reserve, pricing):
        self.session, self.key = session, key
        self.reserve, self.pricing = reserve, pricing
        self.ordinal = 0

    async def invoke(self, handler, kwargs):
        from open_deep_research.agentscope_runtime.model_policy import (
            billed_failure_usage,
            retryable,
        )
        from open_deep_research.agentscope_runtime.recovery import (
            response_dump,
            response_load,
            stable_input,
        )

        model = kwargs["current_model"]
        key = f"{self.key}:attempt:{self.ordinal}"
        self.ordinal += 1
        reserve = dict(self.reserve)
        request = stable_input({k: v for k, v in kwargs.items() if k != "current_model"})
        reserve["input_tokens"] = max(reserve["input_tokens"], len(json.dumps(request, ensure_ascii=False).encode("utf-8")))
        limit = kwargs.get("accounting_max_tokens")
        if limit is not None:
            reserve["output_tokens"] = limit
        pricing = getattr(model, "accounting_price", self.pricing)
        if "cost_micro_usd" in reserve:
            if pricing is None:
                raise ValueError("cost-capped candidate requires a frozen price")
            reserve["cost_micro_usd"] = math.ceil(
                reserve["input_tokens"] * pricing[0] + reserve["output_tokens"] * pricing[1]
            )
        record = await self.session.store.begin_operation(
            self.session.lease, key, "model_attempt",
            {"model": getattr(model, "model", None),
             "request": request},
            reserve=reserve,
        )
        if record["replayed"]:
            receipt = record["result"]
            if receipt["status"] != "completed":
                raise RecordedModelFailure(receipt)
            return response_load(receipt["response"], structured=receipt["structured"])

        async def settle(response=None, error=None):
            observed = response_usage(response)
            billed = billed_failure_usage(error) if error is not None else None
            if billed is not None:
                observed = billed
            complete = error is None
            actual = dict(reserve)
            if observed is not None:
                # A stream failure may only contain a partial usage snapshot.
                actual.update(observed if complete or billed is not None else {
                    name: max(reserve[name], count) for name, count in observed.items()
                })
            metadata = getattr(response, "metadata", {}) or {}
            cost = metadata.get("response_cost_usd")
            if cost is not None or pricing is not None:
                if cost is not None and complete:
                    actual["cost_micro_usd"] = math.ceil(cost * 1_000_000)
                elif pricing is not None:
                    actual["cost_micro_usd"] = math.ceil(
                        actual["input_tokens"] * pricing[0] + actual["output_tokens"] * pricing[1]
                    )
                else:
                    actual["cost_micro_usd"] = math.ceil(cost * 1_000_000)
            usage = getattr(response, "usage", None)
            cached = getattr(usage, "cache_input_tokens", None)
            raw_usage = metadata.get("raw_usage") or (getattr(usage, "metadata", None) or {}).get("raw_usage") or {}
            cached = raw_usage.get("cached_input_tokens", cached if cached else None)
            receipt = {
                "status": "completed" if complete else "failed",
                "operation_id": key,
                "observed_usage": observed,
                "usage_status": "reported" if observed is not None and len(observed) == 2 and (complete or billed is not None) else "estimated",
                # Native ChatUsage defaults to zero; absent provider facts stay unknown.
                "cached_input_tokens": cached,
                "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", None) or None,
                "response_cost_usd": cost,
                "cost_status": "reported" if cost is not None and complete else "estimated" if pricing is not None or cost is not None else "unknown",
                "pricing_micro_usd": pricing,
                "response": response_dump(response) if complete else None,
                "structured": isinstance(response, StructuredResponse),
            }
            if error is not None:
                receipt.update(
                    error_type=type(error).__name__,
                    status_code=getattr(error, "status_code", None) or (503 if retryable(error) else None),
                    code=getattr(error, "code", None),
                    uncertain=getattr(error, "uncertain", False),
                )
            await self.session.store.commit_operation(self.session.lease, key, receipt, actual=actual)
            await self.session.hit("model_attempt_committed")

        try:
            result = await handler(**{k: v for k, v in kwargs.items() if k != "accounting_max_tokens"})
        except BaseException as error:
            await settle(error=error)
            raise
        if isinstance(result, (ChatResponse, StructuredResponse)):
            await settle(result)
            return result

        async def stream():
            last = None
            last_usage = None
            try:
                async with aclosing(result):
                    async for chunk in result:
                        last = deepcopy(chunk)
                        last_usage = chunk.usage or last_usage
                        if last.usage is None:
                            last.usage = last_usage
                        yield chunk
            except BaseException as error:
                await settle(last, error)
                raise
            else:
                if last is None or not last.is_last:
                    error = RuntimeError("model stream ended without a final response")
                    await settle(last, error)
                    raise error
                else:
                    await settle(last)

        return stream()
