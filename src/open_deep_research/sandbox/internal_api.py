"""Authenticated API-owned budget, operation and approval control plane."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

import httpx
from fastapi import APIRouter, HTTPException
from open_deep_research.config_types import RuntimeConfig
from pydantic import BaseModel, ConfigDict, Field

from open_deep_research.budgets import BudgetGate
from open_deep_research.configuration import Configuration
from open_deep_research.events.public import event_publisher_from_config
from open_deep_research.sandbox.approvals import ApprovalKind, SecurityApprovalStore
from open_deep_research.sandbox.crypto import (
    NonceReplayCache,
    SandboxDerivedKeys,
    sign_payload,
    validate_timestamp,
    verify_payload,
)
from open_deep_research.sandbox.egress_ledger_store import (
    EgressClassificationStore,
    RunEgressModeStore,
)
from open_deep_research.sandbox.operations import ModelOperationStore
from open_deep_research.tasks.registry import TaskStatus, get_task_registry

logger = logging.getLogger(__name__)

#: Journal outcome status -> usage_events.response_status semantics.
_GATEWAY_OUTCOME_RESPONSE_STATUS = {
    "completed": "success",
    "uncertain": "unknown_failed",
    "failed": "rejected",
}


class ServiceRequest(BaseModel):
    """Common replay-protected service authentication envelope."""

    model_config = ConfigDict(extra="forbid")

    service_timestamp: float
    service_nonce: str = Field(min_length=16, max_length=256)
    service_signature: str

    def signed_payload(self) -> dict[str, Any]:
        """Return the canonical service-auth payload."""
        return self.model_dump(mode="json", exclude={"service_signature"})


class BudgetReserveRequest(ServiceRequest):
    """Reserve one physical model attempt and its estimated usage."""
    run_id: str
    task_id: str
    fence_token: int
    stage: str
    logical_operation_id: str
    physical_attempt_id: str
    model_name: str
    estimated_input_tokens: int = Field(ge=1)
    estimated_output_tokens: int = Field(ge=1)
    request_digest: str | None = None
    agent_role: str | None = None




class BudgetSettleRequest(ServiceRequest):
    """Settle one physical model attempt with actual usage."""
    run_id: str
    fence_token: int
    physical_attempt_id: str
    model_name: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class BudgetFailRequest(ServiceRequest):
    """Finalize failed model reservations as released or uncertain."""
    run_id: str
    fence_token: int
    physical_attempt_id: str
    uncertain: bool


class ToolBudgetReserveRequest(ServiceRequest):
    """Reserve one logical Gateway tool call."""

    run_id: str
    task_id: str
    fence_token: int
    stage: str
    logical_operation_id: str
    # SQL 权威端点用：按工具元数据决定幂等重放与抓取维度；旧文件账本忽略。
    tool_name: str | None = None
    idempotent: bool | None = None


class ToolBudgetSettleRequest(ServiceRequest):
    """Settle one previously reserved logical Gateway tool call."""

    run_id: str
    fence_token: int
    logical_operation_id: str
    # SQL 权威端点用：结算回执与实测物理抓取数；缺失时保持保守预留。
    outcome: dict[str, Any] | None = None
    fetch_calls: int | None = Field(default=None, ge=0)


class OperationTransitionRequest(ServiceRequest):
    """Advance one durable logical model operation."""
    run_id: str
    fence_token: int
    logical_operation_id: str
    status: Literal["dispatched", "completed", "failed", "uncertain"]
    outcome: dict[str, Any] | None = None
    error_type: str | None = None


class OperationGetRequest(ServiceRequest):
    """Read one logical model operation through authenticated POST."""
    run_id: str
    fence_token: int
    logical_operation_id: str
    request_digest: str | None = None


class UsageReportRequest(ServiceRequest):
    """One gateway tool-side model usage event for the usage projection.

    Tool executions in the Gateway process (search summarization, semantic
    rerank, evidence extraction) have no journal and no local trace store;
    the observability wrapper forwards each recorded usage row here so the
    API process stays the single writer of ``usage_events``.
    """

    run_id: str
    task_id: str = ""
    fence_token: int = Field(ge=1)
    stage: str = "unknown"
    agent_role: str | None = None
    provider: str | None = None
    model: str | None = None
    operation: str | None = None
    event_key: str = Field(min_length=1)
    attempt_index: int = Field(default=1, ge=1)
    duration_ms: int | None = Field(default=None, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    estimated_input_tokens: int = Field(default=0, ge=0)
    estimated_output_tokens: int = Field(default=0, ge=0)
    estimated_total_tokens: int = Field(default=0, ge=0)
    usage_source: str = "provider_reported"
    response_status: str = "success"


class ApprovalCreateRequest(ServiceRequest):
    """Create an idempotent durable security approval."""

    reason: str = Field(default="", max_length=1000)
    run_id: str
    task_id: str
    fence_token: int
    kind: ApprovalKind
    capability: str
    target: dict[str, Any]
    operation_id: str
    expires_at: float
    stage: str = "researching"


class ApprovalWaitRequest(ServiceRequest):
    """Long-poll the run approval index after a version cursor."""
    run_id: str
    fence_token: int
    after_version: int = Field(ge=0)
    timeout_seconds: float = Field(default=25, gt=0, le=25)


class ApprovalConsumeRequest(ServiceRequest):
    """Consume an allow-once approval or validate allow-run reuse."""
    run_id: str
    fence_token: int
    approval_id: str
    operation_id: str


class EgressClassificationLoadRequest(ServiceRequest):
    """Load the run egress classification ledger for Gateway cache warm."""
    run_id: str
    fence_token: int


class EgressClassificationRecordRequest(ServiceRequest):
    """Persist one classifier or human verdict into the run ledger."""
    run_id: str
    task_id: str = ""
    fence_token: int
    entry: dict[str, Any]


class EgressModeGetRequest(ServiceRequest):
    """Read the run runtime egress-mode override (absent means profile)."""
    run_id: str
    fence_token: int


class EgressTargetCheckRequest(EgressModeGetRequest):
    """Observe a network target and read its authoritative human state."""

    capability: str
    target: dict[str, Any]


class EgressHealthRequest(EgressModeGetRequest):
    """Persist classifier budget and health before dispatching model calls."""

    state: dict[str, Any]


class TaskActivityPublishRequest(ServiceRequest):
    """Append one task activity event from the sandbox Gateway.

    The Gateway container runs on a read-only filesystem, so its model-call
    activity is forwarded here and persisted by the API process that owns
    the writable runs directory. Native runs use the signed run fence;
    legacy emitters retain their service-signature authorization.
    """
    model_config = ConfigDict(extra="forbid")

    run_id: str
    fence_token: int = 0
    task_id: str = ""
    event_type: str
    kind: str = "model"
    phase: str = "reasoning"
    status: str = "running"
    title: str = ""
    summary: str = ""
    iteration: int | None = Field(default=None, ge=0)
    duration_ms: int | None = Field(default=None, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)
    dedupe_key: str = ""
    update_run_summary: bool = False


@dataclass(frozen=True, slots=True)
class InternalRunContext:
    """Facts the API must authoritatively resolve for one live run."""

    config: RuntimeConfig
    configurable: Configuration
    fence_token: int
    started_at: float


def _journal_usage_event_kwargs(record: dict[str, Any]) -> dict[str, Any] | None:
    """Map one journal record onto usage-event kwargs (None when unusable)."""
    from open_deep_research.models.resolution import parse_model_spec
    from open_deep_research.observability.tracing import TokenUsage

    outcome = record.get("outcome") if isinstance(record.get("outcome"), dict) else {}
    usage_map = outcome.get("usage") if isinstance(outcome.get("usage"), dict) else {}
    input_tokens = int(usage_map.get("input_tokens") or 0)
    output_tokens = int(usage_map.get("output_tokens") or 0)
    model_id = str(outcome.get("model") or "").strip()
    provider, model = parse_model_spec(model_id) if model_id else (None, None)
    usage = TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        raw_usage=dict(usage_map),
        usage_source=(
            "provider_reported" if input_tokens or output_tokens else "missing"
        ),
        response_status=_GATEWAY_OUTCOME_RESPONSE_STATUS.get(
            str(record.get("status")), "success"
        ),
    )
    logical_operation_id = str(record.get("logical_operation_id") or "")
    return {
        "run_id": str(record.get("run_id") or ""),
        "span_id": logical_operation_id or "gateway-model",
        "provider": provider,
        "model": model,
        "usage": usage,
        "event_key": logical_operation_id or None,
        "attempt_index": max(1, len(record.get("physical_attempts") or [])),
        "stage": str(record.get("stage") or "unknown"),
        "agent_role": str(outcome["role"]) if outcome.get("role") else None,
        "task_id": str(record["task_id"]) if record.get("task_id") else None,
        "operation": "gateway.model",
        "duration_ms": None,
    }


def _write_usage_event(
    recorder: Any,
    *,
    run_id: str,
    span_id: str,
    provider: str | None,
    model: str | None,
    usage: Any,
    event_key: str | None,
    attempt_index: int,
    stage: str,
    agent_role: str | None,
    task_id: str | None,
    operation: str | None,
    duration_ms: int | None,
) -> int | None:
    """Estimate cost and persist one usage row; None when deduplicated."""
    recorder.estimate_usage_cost(usage, provider, model)
    return recorder.store.add_usage(
        run_id,
        span_id,
        provider,
        model,
        usage,
        event_key=event_key,
        attempt_index=attempt_index,
        stage=stage,
        agent_role=agent_role,
        task_id=task_id,
        operation=operation,
        duration_ms=duration_ms,
        response_status=usage.response_status,
    )


def backfill_gateway_usage_event(
    context: InternalRunContext, record: dict[str, Any]
) -> None:
    """Re-enter one journaled gateway model operation into ``usage_events``.

    Worker and Gateway processes deliberately run without a trace store, so
    gateway-mediated model calls otherwise vanish from the usage projection
    even though the budget ledger records them. The authenticated
    ``operations/transition`` endpoint is the single writer that backfills
    the projection from the durable journal outcome. Fail-open: the
    observability backfill must never break the control plane.
    """
    try:
        from open_deep_research.observability.tracing import get_trace_recorder

        recorder = get_trace_recorder(context.config)
        if recorder.store is None:
            return
        # Records without a logical operation id cannot be deduplicated and
        # are skipped: a repeated write would double count the attempt.
        if not str(record.get("logical_operation_id") or "").strip():
            return
        kwargs = _journal_usage_event_kwargs(record)
        if kwargs is not None:
            _write_usage_event(recorder, **kwargs)
    except Exception:  # noqa: BLE001 - observability backfill is fail-open
        logger.warning(
            "Gateway usage backfill failed for run %s operation %s",
            record.get("run_id"),
            record.get("logical_operation_id"),
            exc_info=True,
        )


#: Journal directories already reconciled, keyed by
#: (run_id, file count, max mtime) so repeated usage queries skip file reads.
_RECONCILED_JOURNAL_SIGNATURES: set[tuple[str, int, int]] = set()


def reconcile_run_gateway_usage(
    run_id: str,
    *,
    runs_dir: str,
    config: RuntimeConfig | dict[str, Any] | None = None,
) -> int:
    """Idempotently replay terminal journal operations into ``usage_events``.

    Runs whose transitions predate the transition-time backfill keep complete
    Operation Journals but incomplete usage projections. Event-key dedup
    makes the replay safe to repeat; a stat-based signature cache keeps
    steady-state queries at directory-stat cost. Fail-open by contract.
    """
    try:
        root = Path(runs_dir) / run_id / "sandbox" / "model_operations"
        if not root.is_dir():
            return 0
        files = sorted(root.glob("*.json"))
        max_mtime = max((path.stat().st_mtime_ns for path in files), default=0)
        signature = (run_id, len(files), max_mtime)
        if signature in _RECONCILED_JOURNAL_SIGNATURES:
            return 0
        from open_deep_research.observability.tracing import get_trace_recorder

        recorder = get_trace_recorder(
            config or {"configurable": {"runs_dir": runs_dir}}
        )
        if recorder.store is None:
            return 0
        existing = recorder.store.usage_event_keys(run_id)
        written = 0
        for path in files:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict):
                continue
            if record.get("status") not in {"completed", "failed", "uncertain"}:
                continue
            event_key = str(record.get("logical_operation_id") or "")
            if not event_key or event_key in existing:
                continue
            kwargs = _journal_usage_event_kwargs(record)
            if kwargs is None:
                continue
            if _write_usage_event(recorder, **kwargs):
                written += 1
            existing.add(event_key)
        _RECONCILED_JOURNAL_SIGNATURES.add(signature)
        if written:
            logger.info(
                "Reconciled %d gateway usage rows for run %s", written, run_id
            )
        return written
    except Exception:  # noqa: BLE001 - reconciliation is fail-open
        logger.warning(
            "Gateway usage reconciliation failed for run %s", run_id, exc_info=True
        )
        return 0


def build_internal_sandbox_router(
    resolve_run: Callable[[str], InternalRunContext | None],
    *,
    native_ledger: Callable[[str], Any] | None = None,
    native_root_key: Callable[[], str | None] | None = None,
) -> APIRouter:
    """Build the internal-only, HMAC-authenticated sandbox control router.

    ``native_ledger(run_id)`` 返回原生运行的 ``SQLGatewayLedger`` 或 None。
    提供时，预算与操作端点先按运行归属分派：原生运行结算到 SQL 恢复权威
    （同一物理调用只计一次），旧引擎运行保持文件账本行为不变。
    """
    router = APIRouter(prefix="/internal/sandbox", tags=["sandbox-internal"])
    replay = NonceReplayCache()

    async def native_ledger_for(request):
        if native_ledger is None:
            return None
        ledger = await native_ledger(request.run_id)
        if ledger is None:
            return None
        from open_deep_research.agentscope_runtime.gateway_ledger import authorize_gateway_ledger

        return await authorize_gateway_ledger(
            request, ledger, native_root_key() if native_root_key else None, replay
        )

    def _budget_exhausted(exc: BaseException) -> HTTPException:
        dimension = getattr(exc, "dimension", None)
        return HTTPException(
            429, "budget_exhausted:" + getattr(dimension, "value", "unknown")
        )

    def authorize(request: ServiceRequest, context: InternalRunContext) -> None:
        validate_timestamp(request.service_timestamp)
        root = context.configurable.sandbox_root_signing_key
        if not root:
            raise ValueError("sandbox_unavailable:root_signing_key")
        keys = SandboxDerivedKeys.from_root(root)
        if not verify_payload(request.signed_payload(), request.service_signature, keys.service_auth):
            raise ValueError("sandbox_service_auth_invalid")
        replay.consume(
            f"service:{context.fence_token}",
            request.service_nonce,
            expires_at=time.time() + 60,
        )

    async def authority(request: ServiceRequest, run_id: str, fence_token: int) -> InternalRunContext:
        ledger = await native_ledger_for(request)
        if ledger is not None:
            config = getattr(ledger, "config", None)
            if config is None:
                raise HTTPException(503, "native_resources_unavailable")
            return InternalRunContext(config=config,
                configurable=Configuration.from_runnable_config(config), fence_token=fence_token,
                started_at=ledger.recovery.snapshot.application.get("created_at", time.time()))
        context = resolve_run(run_id)
        if context is None:
            raise HTTPException(status_code=404, detail="run_not_active")
        try:
            authorize(request, context)
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        if fence_token != context.fence_token:
            raise HTTPException(status_code=409, detail="stale_fence")
        return context

    def sync_waiting_tasks(run_id: str, approvals: list[Any]) -> None:
        """Project the durable concurrent queue onto legacy scalar task status."""
        pending_by_task: dict[str, list[Any]] = {}
        for approval in approvals:
            if getattr(approval, "status", None) == "pending":
                pending_by_task.setdefault(str(approval.task_id), []).append(approval)
        registry = get_task_registry()
        for task in registry.list(run_id=run_id):
            pending = pending_by_task.get(task.task_id, [])
            if pending:
                first = pending[0]
                task.pending_domain = str(first.target.get("domain") or "") or None
                task.pending_domain_tool = first.capability
                if task.status == TaskStatus.RUNNING:
                    registry.update_status(
                        task.task_id,
                        TaskStatus.WAITING_FOR_CONFIRMATION,
                    )
            elif task.status == TaskStatus.WAITING_FOR_CONFIRMATION:
                task.pending_domain = None
                task.pending_domain_tool = None
                registry.update_status(task.task_id, TaskStatus.RUNNING)


    @router.post("/budgets/reserve")
    async def reserve_budget(request: BudgetReserveRequest) -> dict[str, Any]:
        ledger = await native_ledger_for(request)
        if ledger is not None:
            try:
                return await ledger.reserve(request)
            except Exception as exc:
                if hasattr(exc, "dimension"):
                    raise _budget_exhausted(exc) from None
                raise
        context = await authority(request, request.run_id, request.fence_token)
        gate = BudgetGate.from_config(
            context.configurable,
            request.run_id,
            started_at=context.started_at,
        )
        await asyncio.to_thread(
            gate.reserve_model_call,
            request.physical_attempt_id,
            estimated_input_tokens=request.estimated_input_tokens,
            estimated_output_tokens=request.estimated_output_tokens,
            model_name=request.model_name,
        )
        store = ModelOperationStore(request.run_id, runs_dir=context.configurable.runs_dir)
        record = await asyncio.to_thread(
            store.reserve,
            task_id=request.task_id,
            stage=request.stage,
            logical_operation_id=request.logical_operation_id,
            physical_attempt_id=request.physical_attempt_id,
        )
        return record.model_dump(mode="json")

    @router.post("/budgets/settle")
    async def settle_budget(request: BudgetSettleRequest) -> dict[str, str]:
        if await native_ledger_for(request) is not None:
            # SQL 权威只认终态回执；中间结算通知不改账。
            return {"status": "awaiting_receipt"}
        context = await authority(request, request.run_id, request.fence_token)
        gate = BudgetGate.from_config(
            context.configurable,
            request.run_id,
            started_at=context.started_at,
        )
        await asyncio.to_thread(
            gate.settle_model_call,
            request.physical_attempt_id,
            input_tokens=request.input_tokens,
            output_tokens=request.output_tokens,
            model_name=request.model_name,
        )
        return {"status": "settled"}

    @router.post("/budgets/fail")
    async def fail_budget(request: BudgetFailRequest) -> dict[str, str]:
        if await native_ledger_for(request) is not None:
            return {"status": "awaiting_receipt"}
        context = await authority(request, request.run_id, request.fence_token)
        gate = BudgetGate.from_config(
            context.configurable,
            request.run_id,
            started_at=context.started_at,
        )
        await asyncio.to_thread(
            gate.fail_model_call,
            request.physical_attempt_id,
            uncertain=request.uncertain,
        )
        return {"status": "uncertain" if request.uncertain else "released"}

    @router.post("/budgets/tool-reserve")
    async def reserve_tool_budget(request: ToolBudgetReserveRequest) -> dict[str, Any]:
        ledger = await native_ledger_for(request)
        if ledger is not None:
            try:
                return await ledger.reserve_tool(request)
            except Exception as exc:
                if hasattr(exc, "dimension"):
                    raise _budget_exhausted(exc) from None
                if type(exc).__name__ == "UnknownOperation":
                    raise HTTPException(409, "tool_operation_unknown") from None
                raise
        context = await authority(request, request.run_id, request.fence_token)
        gate = BudgetGate.from_config(
            context.configurable,
            request.run_id,
            started_at=context.started_at,
        )
        await asyncio.to_thread(
            gate.reserve_tool_call,
            request.logical_operation_id,
        )
        return {"status": "reserved"}

    @router.post("/budgets/tool-settle")
    async def settle_tool_budget(
        request: ToolBudgetSettleRequest,
    ) -> dict[str, Any]:
        ledger = await native_ledger_for(request)
        if ledger is not None:
            try:
                return await ledger.settle_tool(request)
            except Exception as exc:
                if type(exc).__name__ == "RecoveryConflict":
                    raise HTTPException(409, str(exc)) from None
                raise
        context = await authority(request, request.run_id, request.fence_token)
        gate = BudgetGate.from_config(
            context.configurable,
            request.run_id,
            started_at=context.started_at,
        )
        await asyncio.to_thread(
            gate.settle_tool_call,
            request.logical_operation_id,
        )
        return {"status": "settled"}

    @router.post("/operations/get")
    async def get_operation(request: OperationGetRequest) -> dict[str, Any]:
        ledger = await native_ledger_for(request)
        if ledger is not None:
            return await ledger.lookup(
                request.logical_operation_id, request.request_digest
            )
        context = await authority(request, request.run_id, request.fence_token)
        record = await asyncio.to_thread(
            ModelOperationStore(
                request.run_id, runs_dir=context.configurable.runs_dir
            ).get,
            request.logical_operation_id,
        )
        if record is None:
            return {"found": False}
        return {"found": True, "operation": record.model_dump(mode="json")}

    @router.post("/operations/transition")
    async def transition_operation(request: OperationTransitionRequest) -> dict[str, Any]:
        ledger = await native_ledger_for(request)
        if ledger is not None:
            return await ledger.transition(request)
        context = await authority(request, request.run_id, request.fence_token)
        expected = {
            "dispatched": {"reserved"},
            "completed": {"dispatched"},
            "failed": {"reserved", "dispatched"},
            "uncertain": {"reserved", "dispatched"},
        }[request.status]
        record = await asyncio.to_thread(
            ModelOperationStore(
                request.run_id, runs_dir=context.configurable.runs_dir
            ).transition,
            request.logical_operation_id,
            expected=expected,
            status=request.status,
            outcome=request.outcome,
            error_type=request.error_type,
        )
        if request.status in {"completed", "failed", "uncertain"}:
            await asyncio.to_thread(
                backfill_gateway_usage_event,
                context,
                record.model_dump(mode="json"),
            )
        return record.model_dump(mode="json")

    @router.post("/usage/report")
    async def report_tool_usage(request: UsageReportRequest) -> dict[str, Any]:
        """Persist one forwarded gateway tool-side model usage row."""
        context = await authority(request, request.run_id, request.fence_token)
        try:
            from open_deep_research.observability.tracing import (
                TokenUsage,
                get_trace_recorder,
            )

            recorder = get_trace_recorder(context.config)
            if recorder.store is None:
                return {"status": "skipped", "revision": None}
            usage = TokenUsage(
                input_tokens=request.input_tokens,
                output_tokens=request.output_tokens,
                total_tokens=(
                    request.total_tokens
                    or (request.input_tokens + request.output_tokens)
                ),
                cached_input_tokens=request.cached_input_tokens,
                cache_creation_input_tokens=request.cache_creation_input_tokens,
                reasoning_tokens=request.reasoning_tokens,
                estimated_input_tokens=request.estimated_input_tokens,
                estimated_output_tokens=request.estimated_output_tokens,
                estimated_total_tokens=request.estimated_total_tokens,
                usage_source=request.usage_source,
                response_status=request.response_status,
            )
            revision = await asyncio.to_thread(
                _write_usage_event,
                recorder,
                run_id=request.run_id,
                span_id=request.event_key,
                provider=request.provider,
                model=request.model,
                usage=usage,
                event_key=request.event_key,
                attempt_index=request.attempt_index,
                stage=request.stage,
                agent_role=request.agent_role,
                task_id=request.task_id or None,
                operation=request.operation or "gateway.tool",
                duration_ms=request.duration_ms,
            )
            if revision:
                # Private helpers of observability.core are reused to keep the
                # forwarded rows on the same SSE usage-revision stream.
                from open_deep_research.observability.usage_events import (
                    _publish_usage_revision,
                    _run_accounting_status,
                )

                await _publish_usage_revision(
                    context.config,
                    revision,
                    _run_accounting_status(recorder, request.run_id),
                )
            return {"status": "recorded" if revision else "duplicate", "revision": revision}
        except Exception:  # noqa: BLE001 - observability is fail-open
            logger.warning(
                "Gateway tool usage report failed for run %s key %s",
                request.run_id,
                request.event_key,
                exc_info=True,
            )
            return {"status": "failed", "revision": None}

    @router.post("/approvals/request")
    async def request_approval(request: ApprovalCreateRequest) -> dict[str, Any]:
        context = await authority(request, request.run_id, request.fence_token)
        approval = await asyncio.to_thread(
            SecurityApprovalStore(
                request.run_id, runs_dir=context.configurable.runs_dir
            ).request,
            task_id=request.task_id,
            fence_token=request.fence_token,
            kind=request.kind,
            capability=request.capability,
            target=request.target,
            operation_id=request.operation_id,
            expires_at=request.expires_at,
            reason=request.reason,
        )
        task = get_task_registry().get(request.task_id)
        if task is not None and task.run_id == request.run_id:
            task.pending_domain = str(request.target.get("domain") or "") or None
            task.pending_domain_tool = request.capability
            if task.status == TaskStatus.RUNNING:
                get_task_registry().update_status(
                    request.task_id, TaskStatus.WAITING_FOR_CONFIRMATION
                )
        await event_publisher_from_config(context.config).publish(
            "security.approval.required",
            stage=request.stage,
            payload={
                "approval_id": approval.approval_id,
                "task_id": approval.task_id,
                "kind": approval.kind,
                "capability": approval.capability,
                "target": approval.target,
                "status": approval.status,
                "expires_at": approval.expires_at,
                "requested_at": approval.requested_at,
                "version": approval.version,
                "reason": approval.reason,
            },
            dedupe_key=f"security-approval:{approval.approval_id}:required",
        )
        return approval.model_dump(mode="json")

    @router.post("/approvals/wait")
    async def wait_approvals(request: ApprovalWaitRequest) -> dict[str, Any]:
        context = await authority(request, request.run_id, request.fence_token)
        version, approvals = await SecurityApprovalStore(
            request.run_id, runs_dir=context.configurable.runs_dir
        ).wait_for_change(
            request.after_version,
            timeout_seconds=request.timeout_seconds,
        )
        sync_waiting_tasks(request.run_id, approvals)
        for approval in approvals:
            if approval.status == "expired":
                await event_publisher_from_config(context.config).publish(
                    "security.approval.resolved",
                    stage="researching",
                    payload={
                        "approval_id": approval.approval_id,
                        "task_id": approval.task_id,
                        "kind": approval.kind,
                        "capability": approval.capability,
                        "decision": "deny",
                        "status": "expired",
                        "version": approval.version,
                    },
                    dedupe_key=(
                        f"security-approval:{approval.approval_id}:expired:"
                        f"{approval.version}"
                    ),
                )
        return {
            "version": version,
            "approvals": [item.model_dump(mode="json") for item in approvals],
        }

    @router.post("/approvals/consume")
    async def consume_approval(request: ApprovalConsumeRequest) -> dict[str, Any]:
        context = await authority(request, request.run_id, request.fence_token)
        try:
            approval = await asyncio.to_thread(
                SecurityApprovalStore(
                    request.run_id, runs_dir=context.configurable.runs_dir
                ).consume,
                request.approval_id,
                operation_id=request.operation_id,
                expected_fence_token=request.fence_token,
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        _version, approvals = await asyncio.to_thread(
            SecurityApprovalStore(
                request.run_id,
                runs_dir=context.configurable.runs_dir,
            ).list
        )
        sync_waiting_tasks(request.run_id, approvals)
        return approval.model_dump(mode="json")

    @router.post("/egress/classifications/load")
    async def load_egress_classifications(
        request: EgressClassificationLoadRequest,
    ) -> dict[str, Any]:
        """Return the full run classification ledger for cache warm."""
        context = await authority(request, request.run_id, request.fence_token)
        entries = await asyncio.to_thread(
            EgressClassificationStore(
                request.run_id, runs_dir=context.configurable.runs_dir
            ).load
        )
        health = await asyncio.to_thread(EgressClassificationStore(
            request.run_id, runs_dir=context.configurable.runs_dir).classifier_state)
        return {"entries": entries, "health": health}

    @router.post("/egress/target/check")
    async def check_egress_target(request: EgressTargetCheckRequest) -> dict[str, Any]:
        context = await authority(request, request.run_id, request.fence_token)
        store = SecurityApprovalStore(request.run_id, runs_dir=context.configurable.runs_dir)
        return await asyncio.to_thread(store.check_target, request.capability,
                                       request.target, request.fence_token)

    @router.post("/egress/health")
    async def record_egress_health(request: EgressHealthRequest) -> dict[str, Any]:
        context = await authority(request, request.run_id, request.fence_token)
        health = await asyncio.to_thread(EgressClassificationStore(
            request.run_id, runs_dir=context.configurable.runs_dir).save_classifier_state,
            request.state)
        await event_publisher_from_config(context.config).publish(
            "security.egress_health", stage="researching", payload=health,
            dedupe_key=f"egress-health:{request.fence_token}:{health.get('revision', 0)}")
        return health

    @router.post("/egress/classifications/record")
    async def record_egress_classification(
        request: EgressClassificationRecordRequest,
    ) -> dict[str, Any]:
        """Persist one ledger entry and publish its public event."""
        context = await authority(request, request.run_id, request.fence_token)
        store = EgressClassificationStore(
            request.run_id, runs_dir=context.configurable.runs_dir
        )
        try:
            result = await asyncio.to_thread(store.record, request.entry)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if result == "recorded":
            entry = dict(request.entry)
            await event_publisher_from_config(context.config).publish(
                "security.egress_classified",
                stage="researching",
                payload={
                    "domain": entry.get("host", ""),
                    "port": entry.get("port", 0),
                    "verdict": entry.get("verdict", ""),
                    "category": entry.get("category", ""),
                    "risk_tags": list(entry.get("risk_tags", [])),
                    "reason": entry.get("reason", ""),
                    "classifier_model": entry.get("model"),
                    "stage_used": entry.get("source", ""),
                    "source": entry.get("source", ""),
                    "tool": entry.get("tool", ""),
                    "task_id": request.task_id or "",
                    "capability": entry.get("capability"),
                    "fingerprint": entry.get("fingerprint"),
                    "version": entry.get("classified_at", 0),
                },
                dedupe_key=f"security-egress:{entry.get('fingerprint', '')}:{entry.get('classified_at', 0)}",
            )
        return {"status": result}

    @router.post("/egress/mode/get")
    async def get_egress_mode(request: EgressModeGetRequest) -> dict[str, Any]:
        """Return the runtime override; stale-fence overrides read as absent."""
        context = await authority(request, request.run_id, request.fence_token)
        override = await asyncio.to_thread(
            RunEgressModeStore(
                request.run_id, runs_dir=context.configurable.runs_dir
            ).get
        )
        if override is not None and override.fence_token != request.fence_token:
            override = None
        return {"override": override.model_dump(mode="json") if override else None}

    @router.post("/task-activity")
    async def publish_task_activity_internal(
        request: TaskActivityPublishRequest,
    ) -> dict[str, Any]:
        ledger = await native_ledger_for(request)
        if ledger is not None:
            config = getattr(ledger, "config", None)
            if config is None:
                raise HTTPException(503, "native_resources_unavailable")
        else:
            context = resolve_run(request.run_id)
            if context is None:
                raise HTTPException(status_code=404, detail="run_not_active")
            try:
                authorize(request, context)
            except ValueError as exc:
                raise HTTPException(status_code=401, detail=str(exc)) from exc
            config = context.config
        from open_deep_research.events.task_activity import publish_task_activity

        event = await publish_task_activity(
            config,
            request.event_type,
            task_id=request.task_id or None,
            update_run_summary=request.update_run_summary,
            kind=request.kind,  # type: ignore[arg-type]
            phase=request.phase,  # type: ignore[arg-type]
            status=request.status,  # type: ignore[arg-type]
            title=request.title or "任务活动",
            summary=request.summary,
            iteration=request.iteration,
            duration_ms=request.duration_ms,
            payload=request.payload,
            dedupe_key=request.dedupe_key
            or f"gateway:{request.run_id}:{secrets.token_urlsafe(8)}",
        )
        return {"published": event is not None}

    return router


class SandboxInternalClient:
    """Gateway-side authenticated client for the API authority."""

    def __init__(self, base_url: str, root_key: str) -> None:
        """Initialize an authenticated client for one API internal origin."""
        self.base_url = base_url.rstrip("/")
        self.keys = SandboxDerivedKeys.from_root(root_key)

    def signed(self, model_type, **values):
        """Construct and sign one service request with a fresh nonce."""
        request = model_type(
            **values,
            service_timestamp=time.time(),
            service_nonce=secrets.token_urlsafe(24),
            service_signature="pending",
        )
        request.service_signature = sign_payload(
            request.signed_payload(), self.keys.service_auth
        )
        return request

    async def post(self, path: str, request: ServiceRequest) -> dict[str, Any]:
        """POST one signed request and return its JSON object."""
        async with httpx.AsyncClient(base_url=self.base_url, timeout=60) as client:
            response = await client.post(
                path,
                content=request.model_dump_json(),
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            return response.json()

    async def get(self, path: str) -> dict[str, Any] | None:
        """GET a non-mutating internal resource, returning None for 404."""
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30) as client:
            response = await client.get(path)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
