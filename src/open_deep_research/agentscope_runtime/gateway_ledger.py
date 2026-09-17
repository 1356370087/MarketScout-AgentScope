"""Gateway model receipts and research budgets share the recovery SQL transaction."""

import math
import time

from fastapi import APIRouter, HTTPException

from open_deep_research.agentscope_runtime.recovery_store import (
    FenceLost,
    RecoveryConflict,
    digest,
)
from open_deep_research.budgets import BudgetExhausted
from open_deep_research.sandbox.crypto import (
    NonceReplayCache,
    SandboxDerivedKeys,
    validate_timestamp,
    verify_payload,
)
from open_deep_research.sandbox.internal_api import (
    BudgetFailRequest,
    BudgetReserveRequest,
    BudgetSettleRequest,
    OperationGetRequest,
    OperationTransitionRequest,
)


class SQLGatewayLedger:
    """One physical V2 attempt per logical operation; unknown effects stay charged.

    Intermediate settle/fail notifications deliberately do not change accounting.
    Only the terminal receipt atomically changes both usage and operation state.
    This closes the old settle-before-receipt crash window.
    """

    def __init__(self, recovery, catalog):
        self.recovery, self.catalog = recovery, catalog

    @staticmethod
    def key(operation_id):
        return "gateway:model:" + operation_id

    def cost(self, model, usage):
        entry = self.catalog.get(model)
        if entry is None:
            raise ValueError("gateway model has no frozen price")
        return math.ceil(
            (
                usage["input_tokens"] * entry["input_cost_per_token"]
                + usage["output_tokens"] * entry["output_cost_per_token"]
            )
            * 1_000_000
        )

    async def reserve(self, request):
        session = self.recovery
        if not request.request_digest:
            raise ValueError("native ledger requires a V2 request digest")
        usage = {
            "model_calls": 1,
            "input_tokens": request.estimated_input_tokens,
            "output_tokens": request.estimated_output_tokens,
        }
        budget = await session.store.budget(session.lease.run_id, session.lease.user_id)
        if "cost_micro_usd" in budget["limits"]:
            usage["cost_micro_usd"] = self.cost(request.model_name, usage)
        return await session.store.begin_operation(
            session.lease,
            self.key(request.logical_operation_id),
            "gateway:model",
            request.request_digest,
            reserve=usage,
        )

    async def lookup(self, operation_id, request_digest):
        session = self.recovery
        row = await session.store.operation_record(
            session.lease, self.key(operation_id)
        )
        if row is None:
            return {"found": False}
        if row["input_digest"] != digest(request_digest):
            raise RecoveryConflict("gateway operation input changed")
        return {
            "found": True,
            "operation": row["result"]
            if row["state"] == "committed"
            else {"status": "dispatched", "outcome": None},
        }

    async def transition(self, request):
        session = self.recovery
        key = self.key(request.logical_operation_id)
        row = await session.store.operation_record(session.lease, key)
        if row is None:
            raise ValueError("gateway operation was not reserved")
        if request.status == "dispatched":
            return {"status": "dispatched"}
        result = {
            "status": request.status,
            "outcome": request.outcome,
            "error_type": request.error_type,
        }
        if row["state"] == "committed":
            # Store verifies that an idempotent retry has the same receipt.
            await session.store.commit_operation(session.lease, key, result)
            return result
        actual = dict(row["reservation"])
        if request.status == "failed":
            actual = {dimension: 0 for dimension in actual}
        elif request.status == "completed":
            outcome = request.outcome or {}
            usage = outcome.get("usage") or {}
            if (
                usage.get("input_tokens") is not None
                and usage.get("output_tokens") is not None
            ):
                actual.update(
                    input_tokens=usage["input_tokens"],
                    output_tokens=usage["output_tokens"],
                )
                if "cost_micro_usd" in actual:
                    cost = outcome.get("response_cost_usd")
                    actual["cost_micro_usd"] = (
                        math.ceil(cost * 1_000_000)
                        if cost is not None
                        else self.cost(outcome["requested_model"], actual)
                    )
        await session.store.commit_operation(session.lease, key, result, actual=actual)
        return result


def build_gateway_ledger_router(resolve, root_key):
    """Resolve a host-owned live RecoverySession, never trust a caller's owner ID."""
    router = APIRouter(prefix="/internal/sandbox")
    replay = NonceReplayCache()
    signing_key = SandboxDerivedKeys.from_root(root_key).service_auth

    async def authorize(request):
        try:
            validate_timestamp(request.service_timestamp)
            if not verify_payload(
                request.signed_payload(), request.service_signature, signing_key
            ):
                raise ValueError("invalid service signature")
            replay.consume(
                "native-gateway", request.service_nonce, expires_at=time.time() + 60
            )
        except ValueError:
            raise HTTPException(401, "sandbox_service_auth_invalid") from None
        ledger = await resolve(request.run_id)
        if ledger is None:
            raise HTTPException(404, "run_not_active")
        lease = ledger.recovery.lease
        if lease.fence != request.fence_token:
            raise HTTPException(409, "stale_fence")
        try:
            async with ledger.recovery.store.transaction(lease):
                pass
        except FenceLost:
            raise HTTPException(409, "stale_fence") from None
        return ledger

    @router.post("/budgets/reserve")
    async def reserve(request: BudgetReserveRequest):
        try:
            return await (await authorize(request)).reserve(request)
        except BudgetExhausted as exc:
            raise HTTPException(
                429, "budget_exhausted:" + exc.dimension.value
            ) from None

    @router.post("/operations/get")
    async def get(request: OperationGetRequest):
        return await (await authorize(request)).lookup(
            request.logical_operation_id, request.request_digest
        )

    @router.post("/operations/transition")
    async def transition(request: OperationTransitionRequest):
        return await (await authorize(request)).transition(request)

    @router.post("/budgets/settle")
    async def settle(request: BudgetSettleRequest):
        await authorize(request)
        return {"status": "awaiting_receipt"}

    @router.post("/budgets/fail")
    async def fail(request: BudgetFailRequest):
        await authorize(request)
        return {"status": "awaiting_receipt"}

    return router
