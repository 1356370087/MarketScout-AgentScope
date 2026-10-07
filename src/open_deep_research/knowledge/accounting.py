"""Atomic per-user service budgets and physical knowledge-model receipts."""

import asyncio
import json
import math
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar

from open_deep_research.documents.database import get_document_pool
from open_deep_research.documents.identity import document_owner_id

from .credentials import KnowledgeBudgetExceeded, daily_model_call_budget

query_account = ContextVar("knowledge_query_account", default=None)


@asynccontextmanager
async def knowledge_query(owner_id: str, query_id: str):
    """Bind standalone calls to their authenticated user and query."""
    token = query_account.set((document_owner_id(owner_id), query_id))
    try:
        yield
    finally:
        query_account.reset(token)


async def reserve_attempt(operation: str, model: str) -> str | None:
    """Reserve one physical attempt before transport; failed attempts still count."""
    account = query_account.get()
    if account is None:
        return None
    owner, query = account
    attempt = str(uuid.uuid4())
    budget = daily_model_call_budget()
    pool = await get_document_pool()
    async with pool.acquire() as connection, connection.transaction():
        allowed = await connection.fetchval(
            """INSERT INTO knowledge_usage_daily(owner_id,day,attempts)
               VALUES($1::uuid,(now() AT TIME ZONE 'UTC')::date,1)
               ON CONFLICT(owner_id,day) DO UPDATE
                 SET attempts=knowledge_usage_daily.attempts+1
               WHERE $2::bigint=0 OR knowledge_usage_daily.attempts<$2
               RETURNING attempts""", owner, budget)
        if allowed is None:
            raise KnowledgeBudgetExceeded("knowledge_daily_budget_exceeded")
        await connection.execute(
            """INSERT INTO knowledge_model_attempts(id,query_id,owner_id,operation,model,status)
               VALUES($1::uuid,$2::uuid,$3::uuid,$4,$5,'running')""",
            attempt, query, owner, operation, model)
    return attempt


async def settle_attempt(attempt, *, usage=None, error=None):
    """Record usage when known, preserving unknown usage on failures."""
    if attempt is None:
        return
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        await connection.execute(
            """UPDATE knowledge_model_attempts SET status=$2,usage=$3::jsonb,
                 error_code=$4,finished_at=now() WHERE id=$1::uuid""",
            attempt, "unknown" if isinstance(error, (TimeoutError, asyncio.CancelledError)) else "failed" if error else "completed",
            json.dumps(usage) if usage is not None else None,
            type(error).__name__ if error else None)


class ServiceAttemptAccounting:
    """Apply the native model policy's accounting hook without creating a run."""

    def __init__(self, operation):
        self.operation = operation

    async def invoke(self, handler, kwargs):
        from open_deep_research.agentscope_runtime.model_accounting import (
            response_usage,
        )

        attempt = await reserve_attempt(self.operation, kwargs["current_model"].model)
        try:
            result = await handler(**{key: value for key, value in kwargs.items() if key != "accounting_max_tokens"})
        except BaseException as error:
            await settle_attempt(attempt, error=error)
            raise
        usage = response_usage(result)
        pricing = getattr(kwargs["current_model"], "accounting_price", None)
        if usage is not None and pricing is not None:
            usage["estimated_cost_micro_usd"] = math.ceil(usage.get("input_tokens", 0) * pricing[0] + usage.get("output_tokens", 0) * pricing[1])
        await settle_attempt(attempt, usage=usage)
        return result


async def query_usage(query_id: str, owner_id: str, *, persist: bool = False) -> dict:
    """Expose counts and token completeness for the current user's query."""
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            "SELECT operation,status,usage FROM knowledge_model_attempts WHERE query_id=$1::uuid AND owner_id=$2::uuid",
            query_id, document_owner_id(owner_id))
    counts = {}
    tokens = {"input_tokens": 0, "output_tokens": 0}
    known = 0
    cost = 0
    cost_known = 0
    for row in rows:
        counts[row["operation"]] = counts.get(row["operation"], 0) + 1
        usage = row["usage"]
        if usage is not None:
            known += 1
            usage = json.loads(usage) if isinstance(usage, str) else usage
            if usage.get("estimated_cost_micro_usd") is not None:
                cost += usage["estimated_cost_micro_usd"]
                cost_known += 1
            for key in tokens:
                tokens[key] += int(usage.get(key) or 0)
    summary = {"calls": counts, "attempts": len(rows), "reported": tokens if known else None,
            "unknown_usage_attempts": len(rows) - known,
            "cost": cost if rows and cost_known == len(rows) else None,
            "cost_source": "configured_estimate" if rows and cost_known == len(rows) else "unknown"}
    if persist:
        async with pool.acquire() as connection:
            await connection.execute(
                """UPDATE knowledge_queries SET result_digest=jsonb_set(
                    coalesce(result_digest,'{}'::jsonb),'{usage}',$3::jsonb)
                    WHERE id=$1::uuid AND owner_id=$2::uuid""",
                query_id, document_owner_id(owner_id), json.dumps(summary))
    return summary
