"""Gateway model receipts and research budgets share the recovery SQL transaction."""

import math
import time

from fastapi import APIRouter, HTTPException

from open_deep_research.agentscope_runtime.recovery_store import (
    FenceLost,
    RecoveryConflict,
    UnknownOperation,
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
    ToolBudgetReserveRequest,
    ToolBudgetSettleRequest,
)

# 这些工具的物理抓取进入 fetch_calls 维度；其余工具只计 tool_calls。
GATEWAY_FETCH_TOOLS = frozenset({"fetch_url", "fetch_webpage", "web_research"})


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

    @staticmethod
    def tool_key(operation_id):
        return "gateway:tool:" + operation_id

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
            observation={"task_id": request.task_id, "stage": request.stage,
                         "agent_role": request.agent_role, "model": request.model_name},
        )

    async def reserve_tool(self, request):
        """Gateway 执行的工具在 SQL 权威记一次 tool_calls（含抓取维度）。

        幂等重放由请求方的工具元数据决定：非幂等工具在"已执行未结算"窗口
        重试会进入未知隔离，不自动重放外部副作用。
        """
        session = self.recovery
        team = getattr(self, "team", None)
        if team is not None:
            async with team.transport.store.pool.acquire() as db:
                task = await db.fetchrow(
                    """SELECT phase,status,execution_mode FROM research_team_tasks WHERE run_id=$1 AND task_id=$2
                       AND execution_mode IS NOT NULL""", session.lease.run_id, request.task_id,
                )
            if task and task["execution_mode"] == "plan_approval" and task["phase"] in {"planning", "awaiting_plan_review", "awaiting_human"}:
                raise PermissionError("team_plan_approval_required")
            if task and (task["status"] != "running" or task["phase"] != "executing"):
                raise PermissionError("team_task_not_executing")
        if not request.logical_operation_id:
            raise ValueError("gateway tool operation requires a logical id")
        reserve = {"tool_calls": 1}
        if (request.tool_name or "") in GATEWAY_FETCH_TOOLS:
            reserve["fetch_calls"] = 1
        return await session.store.begin_operation(
            session.lease,
            self.tool_key(request.logical_operation_id),
            "gateway:tool",
            {
                "task_id": request.task_id,
                "stage": request.stage,
                "tool_name": request.tool_name,
            },
            replay_safe=request.idempotent is not False,
            reserve=reserve,
            observation={"task_id": request.task_id, "stage": request.stage,
                         "agent_role": "researcher", "tool_name": request.tool_name},
        )

    async def settle_tool(self, request):
        """结算一次 Gateway 工具调用；携带回执后重试可直接复用已提交结果。"""
        session = self.recovery
        key = self.tool_key(request.logical_operation_id)
        row = await session.store.operation_record(session.lease, key)
        if row is None:
            raise ValueError("gateway tool operation was not reserved")
        actual = dict(row["reservation"])
        if (
            request.fetch_calls is not None
            and "fetch_calls" in actual
            and request.fetch_calls >= 0
        ):
            # 实测物理抓取数覆盖保守预留；缺失时保持预留值，不当零。
            actual["fetch_calls"] = request.fetch_calls
        await session.store.commit_operation(
            session.lease,
            key,
            {"status": "tool_settled", "outcome": request.outcome},
            actual=actual,
        )
        return {"status": "settled"}

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
        if request.status in {"failed", "completed"}:
            outcome = request.outcome or {}
            usage = outcome.get("usage") or {}
            rejected = request.status == "failed" and outcome.get("error_code") in {
                "authentication", "permission_denied", "invalid_request",
            }
            if rejected and not any(usage.get(k) is not None for k in ("input_tokens", "output_tokens")):
                # 明确未执行的拒绝可释放 token；请求尝试仍计一次。
                actual = {dimension: 0 for dimension in actual}
                actual["model_calls"] = 1
            else:
                # 部分或缺失用量保留该维度预留，不能以 `or 0` 抹掉未知。
                for dimension in ("input_tokens", "output_tokens"):
                    if usage.get(dimension) is not None:
                        actual[dimension] = usage[dimension]
                cost = outcome.get("response_cost_usd")
                if cost is not None:
                    actual["cost_micro_usd"] = math.ceil(cost * 1_000_000)
                elif outcome.get("requested_model") in self.catalog:
                    actual["cost_micro_usd"] = self.cost(outcome["requested_model"], actual)
        await session.store.commit_operation(session.lease, key, result, actual=actual)
        return result


async def authorize_gateway_ledger(request, ledger, root_key, replay):
    """Share signature, nonce and live lease checks with the compatibility host."""
    try:
        validate_timestamp(request.service_timestamp)
        if not root_key:
            raise ValueError("missing signing key")
        signing_key = SandboxDerivedKeys.from_root(root_key).service_auth
        if not verify_payload(request.signed_payload(), request.service_signature, signing_key):
            raise ValueError("invalid service signature")
        replay.consume("native-gateway", request.service_nonce, expires_at=time.time() + 60)
    except ValueError:
        raise HTTPException(401, "sandbox_service_auth_invalid") from None
    lease = ledger.recovery.lease
    if lease.fence != request.fence_token:
        raise HTTPException(409, "stale_fence")
    try:
        async with ledger.recovery.store.transaction(lease):
            pass
    except FenceLost:
        raise HTTPException(409, "stale_fence") from None
    return ledger


def build_gateway_ledger_router(resolve, root_key):
    """Resolve a host-owned live RecoverySession, never trust a caller's owner ID."""
    router = APIRouter(prefix="/internal/sandbox")
    replay = NonceReplayCache()

    async def authorize(request):
        ledger = await resolve(request.run_id)
        if ledger is None:
            raise HTTPException(404, "run_not_active")
        return await authorize_gateway_ledger(request, ledger, root_key, replay)

    @router.post("/budgets/reserve")
    async def reserve(request: BudgetReserveRequest):
        try:
            return await (await authorize(request)).reserve(request)
        except BudgetExhausted as exc:
            raise HTTPException(
                429, "budget_exhausted:" + exc.dimension.value
            ) from None

    @router.post("/budgets/tool-reserve")
    async def tool_reserve(request: ToolBudgetReserveRequest):
        try:
            return await (await authorize(request)).reserve_tool(request)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        except BudgetExhausted as exc:
            raise HTTPException(
                429, "budget_exhausted:" + exc.dimension.value
            ) from None
        except UnknownOperation:
            raise HTTPException(409, "tool_operation_unknown") from None

    @router.post("/budgets/tool-settle")
    async def tool_settle(request: ToolBudgetSettleRequest):
        try:
            return await (await authorize(request)).settle_tool(request)
        except RecoveryConflict as exc:
            raise HTTPException(409, str(exc)) from None

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
