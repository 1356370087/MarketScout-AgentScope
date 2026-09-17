"""Credential-owning sandbox Gateway for model and governed network operations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import ConfigDict, Field
from open_deep_research.configuration import Configuration
from open_deep_research.models.protocol_errors import ModelGatewayError
from open_deep_research.agentscope_runtime.sandbox_provider import (
    NativeGatewayProvider, STRUCTURED_OUTPUT_TOOL_NAME,
)

from open_deep_research.agentscope_runtime.sandbox_catalog import native_tools_scope
from open_deep_research.sandbox.approvals import SecurityApproval, SecurityApprovalStore
from open_deep_research.sandbox.crypto import (
    NonceReplayCache,
    SandboxDerivedKeys,
    decode_task_token,
    validate_timestamp,
    verify_payload,
)
from open_deep_research.sandbox.egress_classifier import (
    EgressClassificationEntry,
    EgressClassifier,
    EgressClassifierLimits,
    EgressModelCall,
    EgressModelReply,
)
from open_deep_research.sandbox.egress_mode import (
    effective_egress_mode,
    policy_baseline_mode,
)
from open_deep_research.sandbox.internal_api import (
    ApprovalConsumeRequest,
    ApprovalCreateRequest,
    ApprovalWaitRequest,
    BudgetFailRequest,
    BudgetReserveRequest,
    BudgetSettleRequest,
    EgressClassificationLoadRequest,
    EgressClassificationRecordRequest,
    EgressHealthRequest,
    EgressModeGetRequest,
    EgressTargetCheckRequest,
    OperationGetRequest,
    OperationTransitionRequest,
    SandboxInternalClient,
    ServiceRequest,
    ToolBudgetReserveRequest,
    ToolBudgetSettleRequest,
)
from open_deep_research.sandbox.policy import egress_target_from_url
from open_deep_research.sandbox.schema import (
    SandboxProfile,
    command_policy_decision,
    domain_matches,
    filesystem_path_allowed,
    resolve_profile,
    tool_policy_decision,
)
from open_deep_research.sandbox.wire import (
    GatewayCatalogToolV1,
    GatewayModelOutcomeV1,
    GatewayModelOutcomeV2,
    GatewayModelRequestV1,
    GatewayModelRequestV2,
    GatewayOperationLookupOutcomeV1,
    GatewayOperationLookupRequestV1,
    GatewayToolCatalogOutcomeV1,
    GatewayToolCatalogRequestV1,
    GatewayToolOutcomeV1,
    GatewayToolRequestV1,
)
from open_deep_research.tasks.team_bridge import TeamWorkerRequest

logger = logging.getLogger(__name__)
GATEWAY_CREDENTIAL_MAX_TTL_SECONDS = 86_460.0
GATEWAY_CREDENTIAL_SWEEP_SECONDS = 5.0
#: Runtime egress-mode override read cache; narrowing applies within this lag.
_EGRESS_MODE_CACHE_TTL_SECONDS = 5.0


class GatewayRunRegistrationRequest(ServiceRequest):
    """Register one frozen run and its ephemeral OAP credentials."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    fence_token: int = Field(ge=1)
    frozen_config: dict[str, Any]
    api_keys: dict[str, str] = Field(default_factory=dict)
    expires_at: float = Field(gt=0)


class GatewayRunUnregisterRequest(ServiceRequest):
    """Erase one run's in-memory credentials after its ownership epoch ends."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    fence_token: int = Field(ge=1)


@dataclass(slots=True)
class GatewayRunContext:
    """In-memory run configuration and credentials; never serialized by Gateway."""

    config: dict[str, Any]
    fence_token: int
    expires_at: float
    api_keys: dict[str, str] = field(default_factory=dict)
    registered_at: float = field(default_factory=time.time)


def approval_deadline(
    context: GatewayRunContext,
    *,
    timeout_seconds: float,
) -> float:
    """Give each new approval its own window within current credentials."""
    return min(time.time() + timeout_seconds, context.expires_at)


def _wire_message_payload(message: dict[str, Any] | None) -> dict[str, Any] | None:
    """Unwrap the langchain ``{"type": ..., "data": {...}}`` envelope.

    Wire V1 outcomes serialize through ``message_to_dict`` (nested ``data``);
    Wire V2 builds flat OpenAI-style dicts. Both shapes must extract.
    """
    if not isinstance(message, dict):
        return None
    data = message.get("data")
    if isinstance(data, dict) and ("content" in data or "tool_calls" in data):
        return data
    return message


def _wire_message_text(message: dict[str, Any] | None) -> str | None:
    """Extract plain text from one serialized wire message."""
    payload = _wire_message_payload(message)
    if payload is None:
        return None
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        if parts:
            return "".join(parts)
    return None


def _wire_structured_args(message: dict[str, Any] | None) -> dict[str, Any] | None:
    """Extract forced structured-output arguments from one wire message."""
    payload = _wire_message_payload(message)
    if payload is None:
        return None
    calls = payload.get("tool_calls")
    if not isinstance(calls, list) or len(calls) != 1:
        return None
    call = calls[0] if isinstance(calls[0], dict) else {}
    function = call.get("function") or {}
    if function.get("name", call.get("name")) != STRUCTURED_OUTPUT_TOOL_NAME:
        return None
    args = function.get("arguments", call.get("args"))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return None
    return args if isinstance(args, dict) else None


@dataclass(frozen=True)
class EgressPrecheck:
    """Auto-layer decision for one network target before human approval.

    ``ask`` hands the target back to the caller's existing approval flow;
    ``allow``/``deny`` are terminal and carry their decision provenance.
    """

    decision: str  # "allow" | "deny" | "ask"
    source: str = ""


class RemoteEgressLedger:
    """Classification ledger persisted through the API authority."""

    def __init__(
        self,
        *,
        internal: SandboxInternalClient,
        run_id: str,
        fence_token: int,
    ) -> None:
        """Bind the ledger transport to one run ownership epoch."""
        self.internal = internal
        self.run_id = run_id
        self.fence_token = fence_token
        self.health: dict[str, Any] = {}

    async def load(self) -> dict[str, EgressClassificationEntry]:
        """Load every persisted entry keyed by fingerprint."""
        request = self.internal.signed(
            EgressClassificationLoadRequest,
            run_id=self.run_id,
            fence_token=self.fence_token,
        )
        data = await self.internal.post(
            "/internal/sandbox/egress/classifications/load", request
        )
        self.health = data.get("health", {})
        entries = data.get("entries") or {}
        return {
            str(fingerprint): EgressClassificationEntry.from_payload(payload)
            for fingerprint, payload in entries.items()
            if isinstance(payload, dict)
        }

    async def record(self, entry: EgressClassificationEntry) -> None:
        """Persist one entry; transport failures surface to the caller."""
        request = self.internal.signed(
            EgressClassificationRecordRequest,
            run_id=self.run_id,
            fence_token=self.fence_token,
            entry=entry.to_payload(),
        )
        await self.internal.post(
            "/internal/sandbox/egress/classifications/record", request
        )

    async def load_state(self) -> dict[str, Any]:
        """Return counters loaded with the classifier ledger."""
        return self.health

    async def save_state(self, state: dict[str, Any]) -> None:
        """Persist counters before model dispatch."""
        request = self.internal.signed(EgressHealthRequest, run_id=self.run_id,
            fence_token=self.fence_token, state=state)
        await self.internal.post("/internal/sandbox/egress/health", request)


class RemoteBudgetGate:
    """Synchronous BudgetGate-compatible adapter backed by the API authority."""

    def __init__(
        self,
        *,
        internal: SandboxInternalClient,
        run_id: str,
        task_id: str,
        fence_token: int,
        stage: str,
        logical_operation_id: str,
        initial_attempt_count: int = 0,
    ) -> None:
        """Bind one logical operation to the API budget authority."""
        self.internal = internal
        self.run_id = run_id
        self.task_id = task_id
        self.fence_token = fence_token
        self.stage = stage
        self.logical_operation_id = logical_operation_id
        self._counter = max(0, initial_attempt_count)
        self._keys: dict[str, str] = {}
        self._pending_posts: list[tuple[str, ServiceRequest]] = []
        self.last_physical_attempt_id = ""

    def check_deadline(self, stage: str) -> None:
        """Delegate deadline enforcement to the API reserve endpoint."""
        del stage

    def _enqueue(self, path: str, request: ServiceRequest) -> None:
        self._pending_posts.append((path, request))

    async def flush_pending(self) -> None:
        """Await queued authority calls without blocking the Gateway event loop."""
        while self._pending_posts:
            path, request = self._pending_posts[0]
            await self.internal.post(path, request)
            del self._pending_posts[0]

    def reserve_model_call(
        self,
        op_key: str,
        *,
        estimated_input_tokens: int,
        estimated_output_tokens: int,
        model_name: str,
        request_digest: str | None = None,
    ) -> None:
        """Reserve one deterministic physical model attempt before dispatch."""
        self._counter += 1
        physical_id = hashlib.sha256(
            f"{self.logical_operation_id}:{self._counter}".encode()
        ).hexdigest()
        self._keys[op_key] = physical_id
        self.last_physical_attempt_id = physical_id
        request = self.internal.signed(
            BudgetReserveRequest,
            run_id=self.run_id,
            task_id=self.task_id,
            fence_token=self.fence_token,
            stage=self.stage,
            logical_operation_id=self.logical_operation_id,
            physical_attempt_id=physical_id,
            model_name=model_name,
            estimated_input_tokens=max(1, estimated_input_tokens),
            estimated_output_tokens=max(1, estimated_output_tokens),
            request_digest=request_digest,
        )
        self._enqueue("/internal/sandbox/budgets/reserve", request)
        transition = self.internal.signed(
            OperationTransitionRequest,
            run_id=self.run_id,
            fence_token=self.fence_token,
            logical_operation_id=self.logical_operation_id,
            status="dispatched",
            outcome=None,
            error_type=None,
        )
        self._enqueue("/internal/sandbox/operations/transition", transition)

    def settle_model_call(
        self,
        op_key: str,
        *,
        input_tokens: int,
        output_tokens: int,
        model_name: str,
    ) -> None:
        """Settle one physical model attempt with provider usage."""
        physical_id = self._keys[op_key]
        request = self.internal.signed(
            BudgetSettleRequest,
            run_id=self.run_id,
            fence_token=self.fence_token,
            physical_attempt_id=physical_id,
            model_name=model_name,
            input_tokens=max(0, input_tokens),
            output_tokens=max(0, output_tokens),
        )
        self._enqueue("/internal/sandbox/budgets/settle", request)

    def fail_model_call(self, op_key: str, *, uncertain: bool) -> None:
        """Release or conservatively mark a failed physical attempt."""
        physical_id = self._keys.get(op_key)
        if not physical_id:
            return
        request = self.internal.signed(
            BudgetFailRequest,
            run_id=self.run_id,
            fence_token=self.fence_token,
            physical_attempt_id=physical_id,
            uncertain=uncertain,
        )
        self._enqueue("/internal/sandbox/budgets/fail", request)


class GatewayRuntime:
    """Run registry, token verifier and physical model invocation owner."""

    def __init__(self, configurable: Configuration) -> None:
        """Initialize credentials, run registry and API authority client."""
        self.configurable = configurable
        self.keys = SandboxDerivedKeys.from_root(configurable.sandbox_root_signing_key or "")
        self.runs: dict[str, GatewayRunContext] = {}
        self.operation_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self.model_gateways: dict[str, NativeGatewayProvider] = {}
        self.egress_classifiers: dict[str, EgressClassifier] = {}
        self.native_fetch_ledgers = {}
        self._native_model_app = None
        self._egress_classifier_locks: dict[str, asyncio.Lock] = {}
        self._egress_mode_cache: dict[str, tuple[float, str | None]] = {}
        self.nonces = NonceReplayCache()
        self.internal = SandboxInternalClient(
            os.getenv("SANDBOX_API_INTERNAL_URL", "http://api:2024"),
            configurable.sandbox_root_signing_key or "",
        )

    def register(self, request: GatewayRunRegistrationRequest) -> None:
        """Register a signed frozen run and ephemeral credentials in memory."""
        validate_timestamp(request.service_timestamp)
        if not verify_payload(
            request.signed_payload(), request.service_signature, self.keys.service_auth
        ):
            raise ValueError("sandbox_service_auth_invalid")
        self.nonces.consume(
            f"gateway-register:{request.run_id}",
            request.service_nonce,
            expires_at=time.time() + 60,
        )
        now = time.time()
        if request.expires_at <= now:
            raise ValueError("sandbox_gateway_registration_expired")
        if request.expires_at > now + GATEWAY_CREDENTIAL_MAX_TTL_SECONDS + 30:
            raise ValueError("sandbox_gateway_registration_ttl_out_of_range")
        self.evict_expired_runs(now=now)
        existing = self.runs.get(request.run_id)
        if existing is not None and existing.fence_token > request.fence_token:
            raise ValueError("stale_fence")
        if existing is not None and existing.fence_token == request.fence_token:
            current_fingerprint = str(
                existing.config.get("metadata", {}).get("run_config_fingerprint")
                or ""
            )
            next_fingerprint = str(
                request.frozen_config.get("metadata", {}).get(
                    "run_config_fingerprint"
                )
                or ""
            )
            if (
                current_fingerprint
                and next_fingerprint
                and current_fingerprint != next_fingerprint
            ):
                raise ValueError("sandbox_gateway_frozen_config_mismatch")
        if existing is not None and existing.fence_token < request.fence_token:
            self._remove_run_context(request.run_id)
            existing = None
        # A task-level registration refreshes the frozen config and expiry but
        # intentionally carries no model credential.  Replacing the whole map
        # here would erase the Run-scoped LiteLLM key installed by the API just
        # before workers are admitted.  Preserve same-fence credentials and
        # overlay any explicitly refreshed values; a new fence still starts
        # from an empty vault.
        merged_api_keys = (
            dict(existing.api_keys)
            if existing is not None and existing.fence_token == request.fence_token
            else {}
        )
        merged_api_keys.update(request.api_keys)
        config = {
            "configurable": dict(request.frozen_config.get("configurable") or {}),
            "metadata": {
                **dict(request.frozen_config.get("metadata") or {}),
                "sandbox_gateway_physical": True,
                "run_fence_token": request.fence_token,
            },
        }
        # Same-fence task registrations may carry an older copy of the run.
        # Never let them revoke an API-approved fetch extension.
        if existing is not None and existing.fence_token == request.fence_token:
            prior = existing.config.get("metadata", {}).get("fetch_budget_extension", {})
            incoming = config["metadata"].get("fetch_budget_extension", {})
            if int(prior.get("extra_fetches", 0)) > int(incoming.get("extra_fetches", 0)):
                config["metadata"]["fetch_budget_extension"] = dict(prior)
        # Physical observability is returned to the API; Gateway never opens .runs.
        config["configurable"].update(
            {
                "token_usage_accounting_enabled": False,
                "sqlite_observability_enabled": False,
                "event_log_enabled": False,
                "apiKeys": dict(merged_api_keys),
                "_sandbox_credential_vault": {},
            }
        )
        if request.api_keys.get("mcp_subject_token"):
            config["configurable"]["mcp_subject_token"] = request.api_keys[
                "mcp_subject_token"
            ]
        if existing is not None and existing.fence_token == request.fence_token:
            prior_vault = existing.config.get("configurable", {}).get(
                "_sandbox_credential_vault"
            )
            if isinstance(prior_vault, dict):
                config["configurable"]["_sandbox_credential_vault"] = prior_vault
        self.runs[request.run_id] = GatewayRunContext(
            config=config,
            fence_token=request.fence_token,
            api_keys=merged_api_keys,
            registered_at=(
                existing.registered_at
                if existing is not None
                and existing.fence_token == request.fence_token
                else time.time()
            ),
            expires_at=(
                max(existing.expires_at, request.expires_at)
                if existing is not None
                and existing.fence_token == request.fence_token
                else request.expires_at
            ),
        )

    @staticmethod
    def _wipe_run_context(context: GatewayRunContext) -> None:
        """Remove every in-memory reference to per-run credential material."""
        configurable = context.config.get("configurable")
        if isinstance(configurable, dict):
            for key in ("apiKeys", "_sandbox_credential_vault"):
                secret_map = configurable.pop(key, None)
                if isinstance(secret_map, dict):
                    secret_map.clear()
            configurable.pop("mcp_subject_token", None)
        context.api_keys.clear()
        context.config.clear()

    def _remove_run_context(
        self,
        run_id: str,
        *,
        clear_fetch_budget: bool = False,
    ) -> bool:
        """Wipe one run context and all per-operation synchronization state."""
        context = self.runs.pop(run_id, None)
        gateway = self.model_gateways.pop(run_id, None)
        if gateway is not None:
            try:
                asyncio.get_running_loop().create_task(gateway.aclose())
            except RuntimeError:
                pass
        self.egress_classifiers.pop(run_id, None)
        self._egress_classifier_locks.pop(run_id, None)
        self._egress_mode_cache.pop(run_id, None)
        if context is not None:
            self._wipe_run_context(context)
        for key in [key for key in self.operation_locks if key[0] == run_id]:
            self.operation_locks.pop(key, None)
        if clear_fetch_budget:
            from open_deep_research.web.pipeline import clear_run_web_cache

            self.native_fetch_ledgers.pop(run_id, None)
            clear_run_web_cache(run_id)
        return context is not None

    def evict_expired_runs(self, *, now: float | None = None) -> list[str]:
        """Wipe run credentials after their maximum registered task-token TTL."""
        current = time.time() if now is None else now
        expired = sorted(
            run_id
            for run_id, context in self.runs.items()
            if context.expires_at <= current
        )
        for run_id in expired:
            self._remove_run_context(run_id, clear_fetch_budget=True)
        return expired

    async def reap_expired_runs(
        self,
        *,
        interval_seconds: float = GATEWAY_CREDENTIAL_SWEEP_SECONDS,
    ) -> None:
        """Continuously enforce Credential Vault expiry without API cleanup."""
        while True:
            self.evict_expired_runs()
            await asyncio.sleep(max(0.01, interval_seconds))

    def unregister(self, request: GatewayRunUnregisterRequest) -> None:
        """Replay-protected deletion of ephemeral run credentials and locks."""
        validate_timestamp(request.service_timestamp)
        if not verify_payload(
            request.signed_payload(), request.service_signature, self.keys.service_auth
        ):
            raise ValueError("sandbox_service_auth_invalid")
        self.nonces.consume(
            f"gateway-unregister:{request.run_id}",
            request.service_nonce,
            expires_at=time.time() + 60,
        )
        context = self.runs.get(request.run_id)
        if context is not None and context.fence_token != request.fence_token:
            raise ValueError("stale_fence")
        self._remove_run_context(request.run_id, clear_fetch_budget=True)

    def authorize_task(
        self,
        request: (
            GatewayModelRequestV1
            | GatewayModelRequestV2
            | GatewayToolRequestV1
            | GatewayOperationLookupRequestV1
            | GatewayToolCatalogRequestV1
        ),
        *,
        authorization: str,
        timestamp: float,
        nonce: str,
    ) -> tuple[Any, GatewayRunContext]:
        """Validate task claims, timestamp, nonce and live ownership epoch."""
        validate_timestamp(timestamp)
        if not authorization.startswith("Bearer "):
            raise ValueError("sandbox_task_token_missing")
        claims = decode_task_token(authorization[7:], self.keys.task_token)
        self.nonces.consume(claims.jti, nonce, expires_at=claims.expires_at)
        self.evict_expired_runs()
        context = self.runs.get(request.run_id)
        if context is None:
            raise ValueError("sandbox_gateway_run_not_registered")
        if (
            claims.run_id != request.run_id
            or claims.task_id != request.task_id
            or claims.fence_token != context.fence_token
        ):
            raise ValueError("sandbox_task_token_claim_mismatch")
        return claims, context

    def authorize_api_model(
        self,
        request: GatewayModelRequestV1 | GatewayModelRequestV2 | GatewayOperationLookupRequestV1,
        *,
        timestamp: float,
        nonce: str,
        fence_token: int,
        signature: str,
    ) -> GatewayRunContext:
        """Authenticate one trusted API model request with the service key."""
        validate_timestamp(timestamp)
        self.evict_expired_runs()
        context = self.runs.get(request.run_id)
        if context is None:
            raise ValueError("sandbox_gateway_run_not_registered")
        if context.fence_token != fence_token:
            raise ValueError("stale_fence")
        signed = {
            "request": request.model_dump(mode="json"),
            "timestamp": timestamp,
            "nonce": nonce,
            "fence_token": fence_token,
        }
        if not verify_payload(signed, signature, self.keys.service_auth):
            raise ValueError("sandbox_service_auth_invalid")
        self.nonces.consume(
            f"api-model:{request.run_id}:{fence_token}",
            nonce,
            expires_at=time.time() + 60,
        )
        return context

    async def lookup_model_operation(
        self,
        request: GatewayOperationLookupRequestV1,
        context: GatewayRunContext,
    ) -> GatewayOperationLookupOutcomeV1:
        """Read a journaled model outcome without dispatching a Provider call."""
        lookup = self.internal.signed(
            OperationGetRequest,
            run_id=request.run_id,
            fence_token=context.fence_token,
            logical_operation_id=request.logical_operation_id,
        )
        existing = await self.internal.post(
            "/internal/sandbox/operations/get",
            lookup,
        )
        operation = existing.get("operation") if existing.get("found") else None
        raw_outcome = operation.get("outcome") if isinstance(operation, dict) else None
        return GatewayOperationLookupOutcomeV1(
            found=raw_outcome is not None,
            outcome=(
                GatewayModelOutcomeV1.model_validate(raw_outcome)
                if raw_outcome is not None
                else None
            ),
        )

    @native_tools_scope
    async def tool_catalog(
        self,
        request: GatewayToolCatalogRequestV1,
        context: GatewayRunContext,
    ) -> GatewayToolCatalogOutcomeV1:
        """Return permission-filtered Gateway tool schemas, never implementations."""
        from open_deep_research.tools.base import (
            ToolExecutionZone,
            tool_to_model_definition,
        )
        from open_deep_research.tools.governance import (
            AgentRole,
            filter_tools_by_permission,
        )
        from open_deep_research.agentscope_runtime.sandbox_catalog import assembled_tools as assemble_toolset

        role = AgentRole(request.role)
        assembled = await assemble_toolset(role, context.config)
        permitted = filter_tools_by_permission(assembled, role, context.config)
        configuration = Configuration.from_runnable_config(context.config)
        catalog: list[GatewayCatalogToolV1] = []
        for tool in permitted:
            if tool.execution_zone is not ToolExecutionZone.GATEWAY:
                continue
            definition = await tool_to_model_definition(
                tool,
                max_description_chars=configuration.max_tool_description_chars,
            )
            prompt = tool.prompt(context.config)
            catalog.append(
                GatewayCatalogToolV1(
                    name=tool.name,
                    definition=definition,
                    prompt=(
                        str(prompt)[: configuration.max_tool_description_chars]
                        if prompt
                        else None
                    ),
                    origin=getattr(tool.origin, "value", str(tool.origin)),
                    effect=getattr(tool.effect, "value", str(tool.effect)),
                    retryable=tool.retryable,
                    concurrency_safe=tool.concurrency_safe,
                    max_output_chars=tool.max_output_chars,
                )
            )
        if configuration.enable_async_research:
            from open_deep_research.sandbox.internal_api import TeamBridgeRequest
            extra = await self.internal.post("/internal/sandbox/team", self.internal.signed(
                TeamBridgeRequest, run_id=request.run_id, task_id=request.task_id,
                fence_token=context.fence_token, action="catalog", payload={},
            ))
            catalog.extend(GatewayCatalogToolV1.model_validate(item) for item in extra["tools"])
        return GatewayToolCatalogOutcomeV1(tools=catalog)

    async def _wait_for_approval(
        self,
        request: GatewayToolRequestV1,
        context: GatewayRunContext,
        *,
        kind: str,
        capability: str,
        target: dict[str, Any],
        expires_at: float,
    ) -> SecurityApproval:
        """Reuse or durably wait for one run-scoped security decision."""
        fingerprint = SecurityApprovalStore.fingerprint(kind, capability, target)
        current_request = self.internal.signed(
            ApprovalWaitRequest,
            run_id=request.run_id,
            fence_token=context.fence_token,
            after_version=0,
            timeout_seconds=0.1,
        )
        current = await self.internal.post(
            "/internal/sandbox/approvals/wait", current_request
        )
        approval = self._match_reusable_approval(
            current.get("approvals", []),
            fence_token=context.fence_token,
            fingerprint=fingerprint,
            operation_id=request.logical_operation_id,
        )
        if approval is None:
            create = self.internal.signed(
                ApprovalCreateRequest,
                run_id=request.run_id,
                task_id=request.task_id,
                fence_token=context.fence_token,
                kind=kind,
                capability=capability,
                target=target,
                operation_id=request.logical_operation_id,
                expires_at=expires_at,
                stage=request.stage,
            )
            approval = SecurityApproval.model_validate(
                await self.internal.post(
                    "/internal/sandbox/approvals/request", create
                )
            )
            version = int(current.get("version", 0))
            while approval.status == "pending":
                remaining = expires_at - time.time()
                if remaining <= 0:
                    break
                wait = self.internal.signed(
                    ApprovalWaitRequest,
                    run_id=request.run_id,
                    fence_token=context.fence_token,
                    after_version=version,
                    timeout_seconds=min(25.0, remaining),
                )
                update = await self.internal.post(
                    "/internal/sandbox/approvals/wait", wait
                )
                version = int(update.get("version", version))
                match = next(
                    (
                        item
                        for item in update.get("approvals", [])
                        if item.get("approval_id") == approval.approval_id
                    ),
                    None,
                )
                if match is not None:
                    approval = SecurityApproval.model_validate(match)
        if approval.decision in {"allow_once", "allow_run"}:
            await self._post_approval_consumption(request, context, approval)
        if kind == "network" and approval.decision is not None:
            await self._record_network_human_decision(
                request.run_id,
                context,
                host=str(target.get("domain") or ""),
                port=int(target.get("port") or 0),
                allowed=approval.decision in {"allow_once", "allow_run"},
            )
        return approval

    async def _post_approval_consumption(
        self,
        request: GatewayToolRequestV1,
        context: GatewayRunContext,
        approval: SecurityApproval,
    ) -> None:
        """Send the canonical approval-consumption request."""
        consume = self.internal.signed(
            ApprovalConsumeRequest,
            run_id=request.run_id,
            fence_token=context.fence_token,
            approval_id=approval.approval_id,
            operation_id=(
                approval.operation_id
                if approval.decision == "allow_once"
                else request.logical_operation_id
            ),
        )
        await self.internal.post(
            "/internal/sandbox/approvals/consume",
            consume,
        )

    async def _consume_network_approval(
        self,
        request: GatewayToolRequestV1,
        context: GatewayRunContext,
        approval: SecurityApproval,
    ) -> None:
        """Consume an allowed decision and ledger the human network choice."""
        if approval.decision in {"allow_once", "allow_run"} and approval.status != "consumed":
            await self._post_approval_consumption(request, context, approval)
        await self._record_network_human_decision(
            request.run_id,
            context,
            host=str(approval.target.get("domain") or ""),
            port=int(approval.target.get("port") or 0),
            allowed=approval.decision in {"allow_once", "allow_run"},
        )

    async def _request_network_approval(
        self,
        request: GatewayToolRequestV1,
        context: GatewayRunContext,
        *,
        host: str,
        port: int,
        expires_at: float,
        capability: str = "tool.egress",
        consume: bool = True,
        trigger_reason: str = "",
    ) -> tuple[str, SecurityApproval]:
        """Resolve one tool.egress approval without blocking a first request.

        Returns ``(state, approval)`` with state ``allowed`` / ``denied`` /
        ``pending``. The first caller for a fingerprint creates the approval
        and returns ``pending`` immediately so the worker surface can surface
        ``approval_required`` and refund the researcher turn. A caller that
        re-encounters the same still-pending approval blocks for at most
        ``sandbox_egress_pending_wait_seconds`` (bounded by the approval
        deadline) so the retry attaches to the human decision in place.
        """
        configuration = Configuration.from_runnable_config(context.config)
        target = {"domain": host, "port": port}
        operation_key = self._network_approval_operation_key(
            request,
            host=host,
            port=port,
        )
        target_request = self.internal.signed(EgressTargetCheckRequest, run_id=request.run_id,
            fence_token=context.fence_token, capability=capability, target=target)
        target_state = await self.internal.post("/internal/sandbox/egress/target/check", target_request)
        if target_state.get("version", 0):
            operation_key = f"{operation_key}-v{target_state['version']}"
        fingerprint = SecurityApprovalStore.fingerprint("network", capability, target)
        current_request = self.internal.signed(
            ApprovalWaitRequest,
            run_id=request.run_id,
            fence_token=context.fence_token,
            after_version=0,
            timeout_seconds=0.1,
        )
        current = await self.internal.post(
            "/internal/sandbox/approvals/wait", current_request
        )
        approvals = current.get("approvals", [])
        if target_state.get("decision") in {"revoke", "block_run"}:
            approvals = [item for item in approvals if item.get("decision") != "allow_run"]
        version = int(current.get("version", 0))
        reusable = self._match_reusable_network_approval(
            approvals,
            fence_token=context.fence_token,
            fingerprint=fingerprint,
            operation_key=operation_key,
        )
        if reusable is not None:
            if consume:
                await self._consume_network_approval(request, context, reusable)
            return "allowed", reusable
        denied = next(
            (
                SecurityApproval.model_validate(item)
                for item in approvals
                if item.get("target_fingerprint") == fingerprint
                and item.get("fence_token") == context.fence_token
                and item.get("decision") == "deny"
                and item.get("status") in {"resolved", "expired"}
                and self._network_approval_operation_matches(
                    item.get("operation_id"),
                    operation_key,
                )
            ),
            None,
        )
        if denied is not None:
            await self._consume_network_approval(request, context, denied)
            return "denied", denied
        attached = next(
            (
                SecurityApproval.model_validate(item)
                for item in approvals
                if item.get("target_fingerprint") == fingerprint
                and item.get("fence_token") == context.fence_token
                and item.get("status") == "pending"
                and self._network_approval_operation_matches(
                    item.get("operation_id"),
                    operation_key,
                )
            ),
            None,
        )
        if attached is None:
            create = self.internal.signed(
                ApprovalCreateRequest,
                run_id=request.run_id,
                task_id=request.task_id,
                fence_token=context.fence_token,
                kind="network",
                capability=capability,
                target=target,
                operation_id=self._network_approval_operation_id(
                    request,
                    operation_key=operation_key,
                ),
                expires_at=expires_at,
                stage=request.stage,
                reason=trigger_reason,
            )
            created = SecurityApproval.model_validate(
                await self.internal.post("/internal/sandbox/approvals/request", create)
            )
            if created.status == "pending":
                return "pending", created
            if created.decision in {"allow_once", "allow_run"}:
                if consume:
                    await self._consume_network_approval(request, context, created)
                return "allowed", created
            return "denied", created
        approval = attached
        attach_deadline = min(
            expires_at,
            time.time()
            + max(0.0, configuration.sandbox_egress_pending_wait_seconds),
        )
        while approval.status == "pending":
            remaining = attach_deadline - time.time()
            if remaining <= 0:
                break
            wait = self.internal.signed(
                ApprovalWaitRequest,
                run_id=request.run_id,
                fence_token=context.fence_token,
                after_version=version,
                timeout_seconds=min(25.0, remaining),
            )
            update = await self.internal.post("/internal/sandbox/approvals/wait", wait)
            version = int(update.get("version", version))
            match = next(
                (
                    item
                    for item in update.get("approvals", [])
                    if item.get("approval_id") == approval.approval_id
                ),
                None,
            )
            if match is not None:
                approval = SecurityApproval.model_validate(match)
        if approval.status == "pending":
            return "pending", approval
        if approval.decision in {"allow_once", "allow_run"}:
            if consume:
                await self._consume_network_approval(request, context, approval)
            return "allowed", approval
        return "denied", approval

    @staticmethod
    def _network_approval_operation_key(
        request: GatewayToolRequestV1,
        *,
        host: str,
        port: int,
    ) -> str:
        """Hash the semantic operation shared by exact tool-call retries."""
        encoded = json.dumps(
            {
                "task_id": request.task_id,
                "role": request.role,
                "stage": request.stage,
                "tool_name": request.tool_name,
                "arguments": request.arguments,
                "host": host.casefold(),
                "port": port,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _network_approval_operation_id(
        request: GatewayToolRequestV1,
        *,
        operation_key: str,
    ) -> str:
        """Keep retry lineage stable while each physical execution stays unique."""
        attempt = hashlib.sha256(request.logical_operation_id.encode()).hexdigest()
        return f"network:{operation_key}:{attempt}"

    @staticmethod
    def _network_approval_operation_matches(
        operation_id: Any,
        operation_key: str,
    ) -> bool:
        """Return whether an approval belongs to the exact retry lineage."""
        return str(operation_id or "").startswith(f"network:{operation_key}:")

    @classmethod
    def _match_reusable_network_approval(
        cls,
        raw_approvals: list[Any],
        *,
        fence_token: int,
        fingerprint: str,
        operation_key: str,
    ) -> SecurityApproval | None:
        """Match run-wide allows or one exact network-operation retry."""
        return next(
            (
                SecurityApproval.model_validate(item)
                for item in raw_approvals
                if item.get("target_fingerprint") == fingerprint
                and item.get("fence_token") == fence_token
                and (
                    item.get("decision") == "allow_run"
                    or (
                        item.get("decision") == "allow_once"
                        and item.get("status") == "resolved"
                        and cls._network_approval_operation_matches(
                            item.get("operation_id"),
                            operation_key,
                        )
                    )
                )
            ),
            None,
        )

    @staticmethod
    def _match_reusable_approval(
        raw_approvals: list[Any],
        *,
        fence_token: int,
        fingerprint: str,
        operation_id: str,
    ) -> SecurityApproval | None:
        """Find one reusable human decision for a capability fingerprint."""
        return next(
            (
                SecurityApproval.model_validate(item)
                for item in raw_approvals
                if item.get("target_fingerprint") == fingerprint
                and item.get("fence_token") == fence_token
                and (
                    item.get("decision") == "allow_run"
                    or (
                        item.get("decision") == "allow_once"
                        and item.get("status") == "resolved"
                        and item.get("operation_id") == operation_id
                    )
                )
            ),
            None,
        )

    async def _reusable_approval(
        self,
        *,
        run_id: str,
        fence_token: int,
        fingerprint: str,
        operation_id: str,
    ) -> SecurityApproval | None:
        """Query the durable approval index for one reusable decision."""
        current_request = self.internal.signed(
            ApprovalWaitRequest,
            run_id=run_id,
            fence_token=fence_token,
            after_version=0,
            timeout_seconds=0.1,
        )
        current = await self.internal.post(
            "/internal/sandbox/approvals/wait", current_request
        )
        return self._match_reusable_approval(
            current.get("approvals", []),
            fence_token=fence_token,
            fingerprint=fingerprint,
            operation_id=operation_id,
        )

    async def _runtime_egress_override(
        self,
        run_id: str,
        fence_token: int,
    ) -> str | None:
        """Read authoritative mode on every authorization, without positive caching."""
        request = self.internal.signed(EgressModeGetRequest, run_id=run_id,
                                       fence_token=fence_token)
        data = await self.internal.post("/internal/sandbox/egress/mode/get", request)
        override = data.get("override")
        return str(override["mode"]) if isinstance(override, dict) else None

    async def _egress_classifier(
        self,
        run_id: str,
        context: GatewayRunContext,
    ) -> EgressClassifier:
        """Lazily create and warm the run's egress classifier."""
        classifier = self.egress_classifiers.get(run_id)
        if classifier is not None:
            return classifier
        lock = self._egress_classifier_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            classifier = self.egress_classifiers.get(run_id)
            if classifier is not None:
                return classifier
            configuration = Configuration.from_runnable_config(context.config)
            classifier = EgressClassifier(
                EgressClassifierLimits.from_configuration(configuration),
                ledger=RemoteEgressLedger(
                    internal=self.internal,
                    run_id=run_id,
                    fence_token=context.fence_token,
                ),
            )
            await classifier.warm()
            self.egress_classifiers[run_id] = classifier
            return classifier

    async def _record_network_human_decision(
        self,
        run_id: str,
        context: GatewayRunContext,
        *,
        host: str,
        port: int,
        allowed: bool,
        tool: str = "",
    ) -> None:
        """Keep legacy callers from promoting operation approvals to model grants."""
        # The approval index owns human decisions. Never promote an operation
        # decision into the model cache, including allow_once and expired deny.
        return None

    def _egress_model_invoker(
        self,
        context: GatewayRunContext,
        *,
        run_id: str,
        task_id: str,
        stage: str,
    ) -> Any:
        """Build a classifier invoker on the Gateway's governed model path."""

        async def invoke(call: EgressModelCall) -> EgressModelReply:
            configuration = Configuration.from_runnable_config(context.config)
            model, _ = self._role_settings(configuration, "egress_classifier")
            if configuration.model_backend == "litellm":
                request = GatewayModelRequestV2(
                    run_id=run_id,
                    task_id=task_id,
                    role="egress_classifier",
                    stage=stage,
                    logical_operation_id=call.logical_operation_id,
                    model=model,
                    messages=call.messages,
                    structured_schema=call.structured_schema,
                    max_output_tokens=call.max_output_tokens,
                    temperature=call.temperature,
                )
                outcome = await self.invoke_model_operation_v2(request, context)
                if outcome.status != "completed":
                    return EgressModelReply(
                        status="failed", served_model=outcome.served_model
                    )
                # Older V2 journal entries only contain the forced tool call.
                structured = outcome.structured
                if structured is None:
                    structured = _wire_structured_args(outcome.message)
                if structured is not None:
                    return EgressModelReply(
                        status="completed",
                        structured=structured,
                        served_model=outcome.served_model,
                    )
                return EgressModelReply(
                    status="completed",
                    content=_wire_message_text(outcome.message),
                    served_model=outcome.served_model,
                )
            from langchain_core.messages import message_to_dict
            from open_deep_research.models.codec import decode_message

            tools: list[dict[str, Any]] = []
            tool_choice: str | dict[str, Any] | bool | None = None
            if call.structured_schema is not None:
                tools = [
                    {
                        "type": "function",
                        "function": {
                            "name": STRUCTURED_OUTPUT_TOOL_NAME,
                            "description": (
                                "Return the response using the required schema."
                            ),
                            "parameters": call.structured_schema,
                            "strict": False,
                        },
                    }
                ]
                tool_choice = {
                    "type": "function",
                    "function": {"name": STRUCTURED_OUTPUT_TOOL_NAME},
                }
            request = GatewayModelRequestV1(
                run_id=run_id,
                task_id=task_id,
                role="egress_classifier",
                stage=stage,
                logical_operation_id=call.logical_operation_id,
                messages=[message_to_dict(decode_message(message)) for message in call.messages],
                tools=tools,
                tool_choice=tool_choice,
                max_output_tokens=call.max_output_tokens,
                temperature=call.temperature,
            )
            outcome = await self.invoke_model_operation(request, context)
            if outcome.status != "completed":
                return EgressModelReply(status="failed")
            structured = _wire_structured_args(outcome.message)
            if structured is not None:
                return EgressModelReply(
                    status="completed",
                    structured=structured,
                    served_model=getattr(outcome, "model", None),
                )
            return EgressModelReply(
                status="completed",
                content=_wire_message_text(outcome.message),
                served_model=getattr(outcome, "model", None),
            )

        return invoke

    @staticmethod
    def _egress_intent(context: GatewayRunContext) -> str:
        """Read the bounded user-message anchor supplied by the orchestrator."""
        configurable = context.config.get("configurable") or {}
        if not isinstance(configurable, dict):
            return ""
        return str(
            (context.config.get("metadata") or {}).get("sandbox_egress_intent")
            or configurable.get("sandbox_egress_intent")
            or configurable.get("research_brief")
            or ""
        )[:500]

    async def _egress_precheck(
        self,
        *,
        run_id: str,
        task_id: str,
        fence_token: int,
        stage: str,
        host: str,
        port: int,
        tool_name: str,
        capability: str,
        operation_id: str,
        profile: SandboxProfile,
        operation_key: str = "",
    ) -> EgressPrecheck:
        """Run the auto approval layer for one unknown network target.

        Tier 1 reuses durable human decisions, Tier 2 consults the run
        classification ledger, Tier 3 invokes the model classifier. Every
        failure direction lands on ``ask`` so the caller's human approval
        flow stays authoritative.
        """
        context = self.runs.get(run_id)
        if context is None or context.fence_token != fence_token:
            return EgressPrecheck(decision="deny", source="stale_fence")
        policy = profile.network
        if (policy.mode == "offline" or port not in policy.allow_ports
                or any(domain_matches(pattern, host) for pattern in policy.deny_domains)):
            request = self.internal.signed(EgressTargetCheckRequest, run_id=run_id,
                fence_token=fence_token, capability=capability, target={"domain": host, "port": port})
            await self.internal.post("/internal/sandbox/egress/target/check", request)
            return EgressPrecheck(decision="deny", source="policy")
        configuration = Configuration.from_runnable_config(context.config)

        async def authority_state():
            override = await self._runtime_egress_override(run_id, fence_token)
            mode = effective_egress_mode(policy_baseline_mode(policy),
                configuration.sandbox_egress_approval_mode or "profile", override)
            request = self.internal.signed(EgressTargetCheckRequest, run_id=run_id,
                fence_token=fence_token, capability=capability,
                target={"domain": host, "port": port})
            target = await self.internal.post("/internal/sandbox/egress/target/check", request)
            return mode, target

        def human_result(target):
            if target.get("decision") == "block_run":
                return EgressPrecheck(decision="deny", source="human_block")
            for item in target.get("approvals", []):
                if (item.get("operation_id") == operation_id or (operation_key and
                    self._network_approval_operation_matches(item.get("operation_id"),
                        f"{operation_key}-v{target['version']}" if target.get("version", 0) else operation_key))) and item.get("decision") == "deny":
                    return EgressPrecheck(decision="deny", source="operation_denied")
            if target.get("decision") == "allow_run":
                return EgressPrecheck(decision="allow", source="human")
            if target.get("decision") == "revoke":
                return EgressPrecheck(decision="ask", source="human_revoked")
            # Legacy exact allow_run grants are safe only when no newer override exists.
            if not target.get("decision") and any(
                item.get("decision") == "allow_run" and item.get("status") in {"resolved", "consumed"}
                for item in target.get("approvals", [])
            ):
                return EgressPrecheck(decision="allow", source="human")
            return None

        try:
            mode, target = await authority_state()
        except Exception:
            return EgressPrecheck(decision="deny", source="authority_unavailable")
        # The deny baseline still permits explicit administrator allowlist entries.
        trusted = any(domain_matches(pattern, host) for pattern in policy.allow_domains)
        if mode.mode == "deny" and not trusted:
            return EgressPrecheck(decision="deny", source="mode")
        human = human_result(target)
        if human is not None:
            return human if human.decision != "ask" or profile.approval_policy != "never" else EgressPrecheck(decision="deny", source=human.source)
        if trusted:
            return EgressPrecheck(decision="allow", source="policy")
        if mode.mode == "open":
            return EgressPrecheck(decision="allow", source="mode")
        if mode.mode != "auto" or capability != "tool.egress" or tool_name not in {"fetch_url", "fetch_webpage", "web_research"}:
            return EgressPrecheck(decision="deny" if profile.approval_policy == "never" else "ask",
                                  source="mode" if mode.mode == "manual" else "capability_requires_human")
        try:
            classifier = await self._egress_classifier(run_id, context)
            result = await classifier.classify_target(host=host, port=port,
                tool_name=tool_name, capability=capability, intent=self._egress_intent(context),
                invoker=self._egress_model_invoker(context, run_id=run_id, task_id=task_id, stage=stage),
                allow_domains=policy.allow_domains, allow_ports=policy.allow_ports,
                allow_http_methods=policy.allow_http_methods)
            fresh_mode, fresh_target = await authority_state()
        except Exception:
            return EgressPrecheck(decision="deny" if profile.approval_policy == "never" else "ask", source="classifier_unavailable")
        human = human_result(fresh_target)
        if human is not None:
            return human if human.decision != "ask" or profile.approval_policy != "never" else EgressPrecheck(decision="deny", source=human.source)
        if fresh_mode.mode != mode.mode:
            return EgressPrecheck(decision="deny" if fresh_mode.mode == "deny" or profile.approval_policy == "never" else "ask", source="mode_changed")
        verdict = result.verdict
        if verdict == "ask" and profile.approval_policy == "never":
            verdict = "deny"
        return EgressPrecheck(decision=verdict, source=result.detail or "classifier")

    @native_tools_scope
    async def invoke_tool(
        self,
        request: GatewayToolRequestV1,
        context: GatewayRunContext,
    ) -> GatewayToolOutcomeV1:
        """Scope nested tool model calls to the authenticated Run's credential."""
        from open_deep_research.models.credentials_context import bind_run_key, reset_run_key

        token = bind_run_key(context.api_keys.get("LITELLM_RUN_KEY", ""))
        try:
            return await self._invoke_tool(request, context)
        finally:
            reset_run_key(token)

    async def _invoke_tool(
        self,
        request: GatewayToolRequestV1,
        context: GatewayRunContext,
    ) -> GatewayToolOutcomeV1:
        """Execute one authoritative Gateway-zone tool operation."""
        from open_deep_research.tools.governance import (
            AgentRole,
            execute_governed_tool_call_native as execute_governed_tool_call,
        )
        from open_deep_research.agentscope_runtime.sandbox_catalog import assembled_tools as assemble_toolset

        if request.execution_zone != "gateway":
            return GatewayToolOutcomeV1(
                logical_operation_id=request.logical_operation_id,
                tool_call_id=request.tool_call_id,
                status="failed",
                error={
                    "error_type": "tool_execution_zone_mismatch",
                    "message": "Gateway execution RPC requires zone=gateway.",
                },
            )
        role = AgentRole(request.role)
        tools = await assemble_toolset(role, context.config)
        if Configuration.from_runnable_config(context.config).enable_async_research and request.tool_name in {
            "TaskCreate", "TaskGet", "TaskList", "TaskUpdate", "SendMessage",
        }:
            from open_deep_research.sandbox.internal_api import TeamBridgeRequest
            result = await self.internal.post("/internal/sandbox/team", self.internal.signed(
                TeamBridgeRequest, run_id=request.run_id, task_id=request.task_id,
                fence_token=context.fence_token, action="tool", payload=request.model_dump(mode="json"),
            ))
            return GatewayToolOutcomeV1.model_validate(result)
        tools_by_name = {tool.name: tool for tool in tools}
        tool = tools_by_name.get(request.tool_name)
        if tool is None:
            return GatewayToolOutcomeV1(
                logical_operation_id=request.logical_operation_id,
                tool_call_id=request.tool_call_id,
                status="failed",
                error={"error_type": "tool_not_found", "message": "Tool is not registered in Gateway."},
            )
        from open_deep_research.tools.base import ToolExecutionZone

        if tool.execution_zone is not ToolExecutionZone.GATEWAY:
            return GatewayToolOutcomeV1(
                logical_operation_id=request.logical_operation_id,
                tool_call_id=request.tool_call_id,
                status="failed",
                error={
                    "error_type": "tool_execution_zone_mismatch",
                    "message": "Sandbox-local tools require authorization-only RPC.",
                },
            )
        configuration = Configuration.from_runnable_config(context.config)
        _bundle, _profile_id, profile = resolve_profile(configuration)
        effect = getattr(tool.effect, "value", str(tool.effect))
        profile_tool_decision = tool_policy_decision(
            profile,
            tool_name=tool.name,
            effect=effect,
        )
        if profile_tool_decision == "deny" or (
            profile_tool_decision == "ask"
            and profile.approval_policy == "never"
        ):
            return GatewayToolOutcomeV1(
                logical_operation_id=request.logical_operation_id,
                tool_call_id=request.tool_call_id,
                status="failed",
                error={
                    "error_type": "sandbox_tool_policy_denied",
                    "message": f"Tool '{tool.name}' is denied by the sandbox profile.",
                },
            )
        if profile_tool_decision == "ask":
            tool_approval = await self._wait_for_approval(
                request,
                context,
                kind="tool_effect",
                capability=f"tool.execute:{tool.name}",
                target={
                    "tool": tool.name,
                    "effect": effect,
                    "execution_zone": getattr(
                        tool.execution_zone,
                        "value",
                        str(tool.execution_zone),
                    ),
                },
                expires_at=approval_deadline(
                    context,
                    timeout_seconds=profile.resources.approval_timeout_seconds,
                ),
            )
            if tool_approval.decision not in {"allow_once", "allow_run"}:
                return GatewayToolOutcomeV1(
                    logical_operation_id=request.logical_operation_id,
                    tool_call_id=request.tool_call_id,
                    status="failed",
                    error={
                        "error_type": "sandbox_tool_policy_denied",
                        "message": f"Tool '{tool.name}' was denied or approval timed out.",
                    },
                )

        authorized_hosts: list[str] = []
        raw_urls = tool.egress_urls(request.arguments)
        parsed_targets = [egress_target_from_url(url) for url in raw_urls]
        targets = sorted({target for target in parsed_targets if target is not None})
        if any(target is None for target in parsed_targets):
            return GatewayToolOutcomeV1(
                logical_operation_id=request.logical_operation_id,
                tool_call_id=request.tool_call_id,
                status="failed",
                error={
                    "error_type": "egress_target_invalid",
                    "message": "Tool declared an invalid outbound target.",
                },
            )
        from open_deep_research.security.network import validate_public_http_url

        try:
            for raw_url in raw_urls:
                await validate_public_http_url(raw_url)
        except ValueError as exc:
            return GatewayToolOutcomeV1(
                logical_operation_id=request.logical_operation_id,
                tool_call_id=request.tool_call_id,
                status="failed",
                error={
                    "error_type": "sandbox_private_destination_denied",
                    "message": str(exc)[:500],
                },
            )
        readonly_fetch = tool.name in {"fetch_url", "fetch_webpage", "web_research"}
        egress_capability = "tool.egress" if readonly_fetch else "tool.network"
        if targets:
            for host, port in targets:
                precheck = await self._egress_precheck(
                    run_id=request.run_id,
                    task_id=request.task_id,
                    fence_token=context.fence_token,
                    stage=request.stage,
                    host=host,
                    port=port,
                    tool_name=tool.name,
                    capability=egress_capability,
                    operation_id=request.logical_operation_id,
                    profile=profile,
                    operation_key=self._network_approval_operation_key(request, host=host, port=port),
                )
                if precheck.decision == "allow":
                    authorized_hosts.append(host)
                    continue
                if precheck.decision == "deny":
                    return GatewayToolOutcomeV1(
                        logical_operation_id=request.logical_operation_id,
                        tool_call_id=request.tool_call_id,
                        status="failed",
                        error={
                            "error_type": "egress_domain_denied",
                            "message": (
                                f"Target '{host}:{port}' was denied by the "
                                f"sandbox egress policy ({precheck.source})."
                            ),
                            "domain": host,
                            "port": port,
                        },
                    )
                approval_state, approval = await self._request_network_approval(
                    request,
                    context,
                    host=host,
                    port=port,
                    consume=False,
                    capability=egress_capability,
                    trigger_reason=precheck.source,
                    expires_at=approval_deadline(
                        context,
                        timeout_seconds=profile.resources.approval_timeout_seconds,
                    ),
                )
                if approval_state == "pending":
                    return GatewayToolOutcomeV1(
                        logical_operation_id=request.logical_operation_id,
                        tool_call_id=request.tool_call_id,
                        status="approval_required",
                        approval_id=approval.approval_id,
                        error={
                            "error_type": "egress_domain_pending",
                            "message": (
                                f"Domain '{host}' awaits a human network "
                                "approval. Re-issue the same call to wait for "
                                "the decision; do not switch objective."
                            ),
                            "domain": host,
                            "port": port,
                        },
                    )
                if approval_state == "denied":
                    return GatewayToolOutcomeV1(
                        logical_operation_id=request.logical_operation_id,
                        tool_call_id=request.tool_call_id,
                        status="failed",
                        error={
                            "error_type": "egress_domain_denied",
                            "message": f"Domain '{host}' was denied or approval timed out.",
                            "domain": host,
                            "port": port,
                        },
                    )
                authorized_hosts.append(host)
        budget_request = self.internal.signed(
            ToolBudgetReserveRequest,
            run_id=request.run_id,
            task_id=request.task_id,
            fence_token=context.fence_token,
            stage=request.stage,
            logical_operation_id=request.logical_operation_id,
        )
        await self.internal.post(
            "/internal/sandbox/budgets/tool-reserve", budget_request
        )
        call = {
            "name": request.tool_name,
            "args": request.arguments,
            "id": request.tool_call_id,
        }
        execution_config = {
            **context.config,
            "metadata": {
                **context.config.get("metadata", {}),
                # The registered context is frozen once per Run and therefore
                # cannot carry the per-Worker task identity.  The authenticated
                # RPC request is authoritative for both budget dimensions.
                "run_id": request.run_id,
                "task_id": request.task_id,
                "run_fence_token": context.fence_token,
                "sandbox_tool_stage": request.stage,
                "research_wave_id": request.wave_id,
                "sandbox_gateway_authorized_hosts": authorized_hosts,
            },
        }
        from open_deep_research.sandbox.egress_context import egress_authorizer

        once_grants: dict[tuple[str, int, str], int] = {}

        async def authorize_nested(url: str, capability: str, consume: bool = False) -> str:
            target = egress_target_from_url(url)
            if target is None:
                return "deny"
            host, port = target
            try:
                await validate_public_http_url(url)
            except ValueError:
                return "deny"
            if capability == "tool.egress" and "GET" not in profile.network.allow_http_methods:
                return "deny"
            nested_request = request if capability in {"tool.egress", "tool.network"} else request.model_copy(
                update={"tool_name": f"{request.tool_name}:{capability}"})
            operation_key = self._network_approval_operation_key(nested_request, host=host, port=port)
            precheck = await self._egress_precheck(run_id=request.run_id, task_id=request.task_id,
                fence_token=context.fence_token, stage=request.stage, host=host, port=port,
                tool_name=tool.name, capability=capability, operation_id=request.logical_operation_id,
                profile=profile, operation_key=operation_key)
            if precheck.decision != "ask":
                return precheck.decision
            authority_request = self.internal.signed(EgressTargetCheckRequest, run_id=request.run_id,
                fence_token=context.fence_token, capability=capability, target={"domain": host, "port": port})
            authority = await self.internal.post("/internal/sandbox/egress/target/check", authority_request)
            if authority.get("decision") == "block_run":
                return "deny"
            grant_key = (host, port, capability)
            if once_grants.get(grant_key) == authority.get("version", 0):
                return "allow"
            state, approval = await self._request_network_approval(nested_request, context,
                host=host, port=port, capability=capability, consume=consume,
                trigger_reason=precheck.source,
                expires_at=approval_deadline(context, timeout_seconds=profile.resources.approval_timeout_seconds))
            if state != "allowed":
                return "ask" if state == "pending" else "deny"
            # Re-read authority after any human wait, including revocation races.
            fresh = await self.internal.post("/internal/sandbox/egress/target/check", authority_request)
            if fresh.get("decision") == "block_run" or (fresh.get("decision") == "revoke"
                and fresh.get("version", 0) != authority.get("version", 0)):
                return "deny"
            latest = await self._egress_precheck(run_id=request.run_id, task_id=request.task_id,
                fence_token=context.fence_token, stage=request.stage, host=host, port=port,
                tool_name=tool.name, capability=capability, operation_id=request.logical_operation_id,
                profile=profile, operation_key=operation_key)
            if latest.decision == "deny":
                return "deny"
            if consume and approval.decision == "allow_once":
                once_grants[grant_key] = fresh.get("version", 0)
            return "allow"

        async def execute_authorized():
            token = egress_authorizer.set(authorize_nested)
            try:
                # Generic tools cannot invoke our HTTP callback themselves. Consume
                # their separate manual capability immediately before delegation.
                if not readonly_fetch:
                    for raw_url in raw_urls:
                        decision = await authorize_nested(raw_url, egress_capability, True)
                        if decision != "allow":
                            return GatewayToolOutcomeV1(
                                logical_operation_id=request.logical_operation_id,
                                tool_call_id=request.tool_call_id,
                                status="failed",
                                error={"error_type": "egress_domain_denied",
                                       "message": "Network authorization changed before tool execution."},
                            )
                return await execute_governed_tool_call(
                    call,
                    tools_by_name,
                    role,
                    execution_config,
                    operation_id=request.logical_operation_id,
                )
            finally:
                egress_authorizer.reset(token)

        governed = await execute_authorized()
        if isinstance(governed, GatewayToolOutcomeV1):
            return governed
        if (
            governed.error is not None
            and governed.error.error_type.value == "interaction_required"
            and governed.error.detail.get("interaction_url")
        ):
            interaction_url = str(governed.error.detail["interaction_url"])
            approval = await self._wait_for_approval(
                request,
                context,
                kind="mcp_oauth",
                capability=f"mcp.oauth:{request.tool_name}",
                target={"url": interaction_url, "tool": request.tool_name},
                expires_at=approval_deadline(
                    context,
                    timeout_seconds=profile.resources.approval_timeout_seconds,
                ),
            )
            if approval.decision in {"allow_once", "allow_run"}:
                governed = await execute_authorized()
                if isinstance(governed, GatewayToolOutcomeV1):
                    return governed
        settle_request = self.internal.signed(
            ToolBudgetSettleRequest,
            run_id=request.run_id,
            fence_token=context.fence_token,
            logical_operation_id=request.logical_operation_id,
        )
        await self.internal.post(
            "/internal/sandbox/budgets/tool-settle",
            settle_request,
        )
        if governed.error is not None:
            return GatewayToolOutcomeV1(
                logical_operation_id=request.logical_operation_id,
                tool_call_id=request.tool_call_id,
                status="failed",
                error=governed.error.model_dump(mode="json"),
            )
        return GatewayToolOutcomeV1(
            logical_operation_id=request.logical_operation_id,
            tool_call_id=request.tool_call_id,
            status="completed",
            output=governed.result.output if governed.result is not None else governed.message.content,
        )

    @native_tools_scope
    async def authorize_local_tool(
        self,
        request: GatewayToolRequestV1,
        context: GatewayRunContext,
    ) -> GatewayToolOutcomeV1:
        """Authorize, but never physically execute, a sandbox-local tool call."""
        from open_deep_research.security.redaction import redact_text
        from open_deep_research.tools.base import ToolExecutionZone
        from open_deep_research.tools.governance import (
            AgentRole,
            filter_tools_by_permission,
        )
        from open_deep_research.agentscope_runtime.sandbox_catalog import assembled_tools as assemble_toolset

        if request.execution_zone != "sandbox_local":
            return GatewayToolOutcomeV1(
                logical_operation_id=request.logical_operation_id,
                tool_call_id=request.tool_call_id,
                status="failed",
                error={
                    "error_type": "tool_execution_zone_mismatch",
                    "message": "Local authorization RPC requires zone=sandbox_local.",
                },
            )
        role = AgentRole(request.role)
        tools = await assemble_toolset(role, context.config)
        tool = next((item for item in tools if item.name == request.tool_name), None)
        if tool is None or tool.execution_zone is not ToolExecutionZone.SANDBOX_LOCAL:
            return GatewayToolOutcomeV1(
                logical_operation_id=request.logical_operation_id,
                tool_call_id=request.tool_call_id,
                status="failed",
                error={
                    "error_type": "tool_execution_zone_mismatch",
                    "message": "Tool is not registered as sandbox-local.",
                },
            )
        permitted = filter_tools_by_permission([tool], role, context.config)
        if not permitted:
            return GatewayToolOutcomeV1(
                logical_operation_id=request.logical_operation_id,
                tool_call_id=request.tool_call_id,
                status="failed",
                error={
                    "error_type": "permission_denied",
                    "message": "Caller is not permitted to use this local tool.",
                },
            )
        try:
            tool.input_schema.model_validate(request.arguments)
        except Exception as exc:  # noqa: BLE001 - return a bounded schema denial
            return GatewayToolOutcomeV1(
                logical_operation_id=request.logical_operation_id,
                tool_call_id=request.tool_call_id,
                status="failed",
                error={
                    "error_type": "tool_input_invalid",
                    "message": str(exc)[:500],
                },
            )

        configuration = Configuration.from_runnable_config(context.config)
        _bundle, _profile_id, profile = resolve_profile(configuration)
        effect = getattr(tool.effect, "value", str(tool.effect))
        decisions = [
            tool_policy_decision(
                profile,
                tool_name=tool.name,
                effect=effect,
            )
        ]
        approval_kind = "tool_effect"
        target: dict[str, Any] = {
            "tool": tool.name,
            "effect": effect,
            "arguments_digest": hashlib.sha256(
                json.dumps(
                    request.arguments,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        }
        if tool.name == "ShellExec":
            command = str(request.arguments.get("command") or "")
            decisions.append(command_policy_decision(profile, command))
            approval_kind = "command"
            target.update(
                {
                    "command_preview": redact_text(command)[:1000],
                    "cwd": str(request.arguments.get("cwd") or ".")[:1024],
                }
            )
        elif tool.name in {"ReadFile", "WriteFile"}:
            path = str(request.arguments.get("path") or "")
            write = tool.name == "WriteFile"
            if not filesystem_path_allowed(profile, path, write=write):
                decisions.append("deny")
            approval_kind = "filesystem"
            target.update(
                {
                    "path": path[:1024],
                    "operation": "write" if write else "read",
                }
            )

        decision = (
            "deny"
            if "deny" in decisions
            else "ask"
            if "ask" in decisions
            else "allow"
        )
        if decision == "ask" and profile.approval_policy == "never":
            decision = "deny"
        if decision == "deny":
            return GatewayToolOutcomeV1(
                logical_operation_id=request.logical_operation_id,
                tool_call_id=request.tool_call_id,
                status="failed",
                error={
                    "error_type": "sandbox_local_tool_denied",
                    "message": f"Local tool '{tool.name}' is denied by the sandbox profile.",
                },
            )
        if decision == "ask":
            approval = await self._wait_for_approval(
                request,
                context,
                kind=approval_kind,
                capability=f"tool.execute:{tool.name}",
                target=target,
                expires_at=approval_deadline(
                    context,
                    timeout_seconds=profile.resources.approval_timeout_seconds,
                ),
            )
            if approval.decision not in {"allow_once", "allow_run"}:
                return GatewayToolOutcomeV1(
                    logical_operation_id=request.logical_operation_id,
                    tool_call_id=request.tool_call_id,
                    status="failed",
                    error={
                        "error_type": "sandbox_local_tool_denied",
                        "message": f"Local tool '{tool.name}' was denied or approval timed out.",
                    },
                )
        budget_request = self.internal.signed(
            ToolBudgetReserveRequest,
            run_id=request.run_id,
            task_id=request.task_id,
            fence_token=context.fence_token,
            stage=request.stage,
            logical_operation_id=request.logical_operation_id,
        )
        await self.internal.post(
            "/internal/sandbox/budgets/tool-reserve", budget_request
        )
        settle_request = self.internal.signed(
            ToolBudgetSettleRequest,
            run_id=request.run_id,
            fence_token=context.fence_token,
            logical_operation_id=request.logical_operation_id,
        )
        await self.internal.post(
            "/internal/sandbox/budgets/tool-settle",
            settle_request,
        )
        return GatewayToolOutcomeV1(
            logical_operation_id=request.logical_operation_id,
            tool_call_id=request.tool_call_id,
            status="completed",
            output={
                "authorized": True,
                "execution_zone": ToolExecutionZone.SANDBOX_LOCAL.value,
            },
        )

    @staticmethod
    def _failure_usage(exc: BaseException) -> dict[str, int]:
        """Extract billed usage a provider exception still carries.

        Client-side truncation errors (for example OpenAI SDK
        ``LengthFinishReasonError``) hold the finished completion with its
        usage; recording it keeps failed-operation token costs visible in the
        run usage accounting instead of silently disappearing.
        """
        completion = getattr(exc, "completion", None)
        usage = getattr(completion, "usage", None)
        if usage is None:
            return {}
        try:
            input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        except (TypeError, ValueError):
            return {}
        if input_tokens <= 0 and output_tokens <= 0:
            return {}
        return {"input_tokens": input_tokens, "output_tokens": output_tokens}

    @staticmethod
    def _role_settings(configuration: Configuration, role: str) -> tuple[str, int]:
        mapping = {
            "supervisor": (configuration.research_model, configuration.research_model_max_tokens),
            "researcher": (configuration.research_model, configuration.research_model_max_tokens),
            "summarization": (configuration.summarization_model, configuration.summarization_model_max_tokens),
            "message_summary": (configuration.message_summary_model, configuration.message_summary_model_max_tokens),
            "compression": (configuration.compression_model, configuration.compression_model_max_tokens),
            "final_report": (configuration.final_report_model, configuration.final_report_model_max_tokens),
            "report_review": (
                configuration.report_review_model or configuration.quality_evaluation_model,
                configuration.report_review_model_max_tokens,
            ),
            # Query spans use descriptive reviewer/revisor names while the
            # frozen configuration role is ``report_review``.  Keep both
            # aliases on the sandbox boundary so per-run Gateway execution
            # receives the same model and token budget as the API process.
            "report_reviewer": (
                configuration.report_review_model or configuration.quality_evaluation_model,
                configuration.report_review_model_max_tokens,
            ),
            "report_revisor": (
                configuration.final_report_model,
                configuration.final_report_model_max_tokens,
            ),
            "quality_evaluation": (
                configuration.quality_evaluation_model,
                configuration.quality_evaluation_model_max_tokens,
            ),
            "quality_evaluator": (
                configuration.quality_evaluation_model,
                configuration.quality_evaluation_model_max_tokens,
            ),
            "egress_classifier": (
                configuration.egress_classifier_model
                or configuration.quality_evaluation_model,
                configuration.quality_evaluation_model_max_tokens,
            ),
        }
        mapping.update({
            "web_rerank": (configuration.web_rerank_model or configuration.summarization_model, configuration.summarization_model_max_tokens),
            "web_evidence": (configuration.web_evidence_model or configuration.summarization_model, configuration.summarization_model_max_tokens),
        })
        if role not in mapping or not mapping[role][0]:
            raise ValueError(f"sandbox_gateway_unknown_model_role:{role}")
        return str(mapping[role][0]), int(mapping[role][1])

    @staticmethod
    def _usage(message: Any) -> dict[str, int]:
        usage = getattr(message, "usage_metadata", None) or {}
        response_usage = message.response_metadata.get("token_usage", {})
        return {
            "input_tokens": int(usage.get("input_tokens", response_usage.get("prompt_tokens", 0)) or 0),
            "output_tokens": int(usage.get("output_tokens", response_usage.get("completion_tokens", 0)) or 0),
        }

    async def invoke_model_operation(
        self,
        request: GatewayModelRequestV1,
        context: GatewayRunContext,
    ) -> GatewayModelOutcomeV1:
        """Serialize duplicate logical operations before consulting the journal."""
        key = (request.run_id, request.logical_operation_id)
        lock = self.operation_locks.setdefault(key, asyncio.Lock())
        async with lock:
            return await self._invoke_model_operation_locked(request, context)

    async def _invoke_model_operation_locked(
        self,
        request: GatewayModelRequestV1,
        context: GatewayRunContext,
    ) -> GatewayModelOutcomeV1:
        """Execute or recover one idempotent logical model operation."""
        from langchain_core.messages import AIMessage, message_to_dict, messages_from_dict
        from open_deep_research.models.fallback import ModelErrorKind, classify_model_error, invoke_with_model_fallback
        from open_deep_research.models.resolution import build_model_config, get_configurable_model_template
        from open_deep_research.observability import apply_helicone_config, invoke_model_with_retry_observability
        lookup = self.internal.signed(
            OperationGetRequest,
            run_id=request.run_id,
            fence_token=context.fence_token,
            logical_operation_id=request.logical_operation_id,
        )
        existing = await self.internal.post("/internal/sandbox/operations/get", lookup)
        prior_attempt_count = 0
        if existing.get("found"):
            operation = existing["operation"]
            prior_attempt_count = len(operation.get("physical_attempts") or [])
            if operation.get("status") == "completed" and operation.get("outcome"):
                return GatewayModelOutcomeV1.model_validate(operation["outcome"])
            if operation.get("status") == "uncertain" and operation.get("outcome"):
                return GatewayModelOutcomeV1.model_validate(operation["outcome"])
            if operation.get("status") == "failed" and operation.get("outcome"):
                failed = GatewayModelOutcomeV1.model_validate(operation["outcome"])
                if failed.error_type not in {
                    ModelErrorKind.RATE_LIMITED.value,
                    ModelErrorKind.TRANSIENT.value,
                    ModelErrorKind.MODEL_UNAVAILABLE.value,
                }:
                    return failed
            if operation.get("status") == "dispatched":
                return GatewayModelOutcomeV1(
                    logical_operation_id=request.logical_operation_id,
                    physical_attempt_id=str(operation.get("physical_attempt_id") or "unknown"),
                    status="uncertain",
                    error_type="model_operation_uncertain",
                    error_message="A prior physical dispatch has no terminal outcome.",
                )
        configuration = Configuration.from_runnable_config(context.config)
        primary_model, max_tokens = self._role_settings(configuration, request.role)
        if request.role == "egress_classifier" and request.max_output_tokens is not None:
            max_tokens = min(max_tokens, request.max_output_tokens)
        messages = messages_from_dict(request.messages)
        fallback_events: list[dict[str, Any]] = []
        # Last candidate the fallback chain attempted; on success this is the
        # model that actually served the call and is echoed for usage backfill.
        selected_model: list[str] = []
        budget = RemoteBudgetGate(
            internal=self.internal,
            run_id=request.run_id,
            task_id=request.task_id,
            fence_token=context.fence_token,
            stage=request.stage,
            logical_operation_id=request.logical_operation_id,
            initial_attempt_count=prior_attempt_count,
        )

        async def call(model_id: str, call_messages: list[Any]) -> AIMessage:
            selected_model.clear()
            selected_model.append(model_id)
            fake_provider = os.getenv(
                "SANDBOX_GATEWAY_FAKE_PROVIDER", "false"
            ).lower() in {"1", "true", "yes", "on"}
            if fake_provider:
                from open_deep_research.sandbox.fake_provider import (
                    DeterministicGatewayModel,
                )

                model = DeterministicGatewayModel(role=request.role)
            else:
                model = get_configurable_model_template()
            if request.tools and hasattr(model, "bind_tools"):
                model = model.bind_tools(
                    request.tools,
                    tool_choice=request.tool_choice,
                )
            if request.model_kwargs and hasattr(model, "bind"):
                model = model.bind(**request.model_kwargs)
            model_config = (
                {}
                if fake_provider
                else apply_helicone_config(
                    build_model_config(
                        model_id,
                        max_tokens,
                        context.config,
                        role=request.role,
                        temperature=(request.temperature if request.role == "egress_classifier" else
                            configuration.quality_evaluation_temperature
                            if request.role
                            in {"quality_evaluation", "quality_evaluator"}
                            else (
                                configuration.report_review_temperature
                                if request.role
                                in {"report_review", "report_reviewer", "report_revisor"}
                                else None
                            )
                        ),
                    ),
                    context.config,
                    span_name=f"gateway.{request.role}.model",
                    agent_role=request.role,
                )
            )
            if hasattr(model, "with_config"):
                model = model.with_config(model_config)
            response = await invoke_model_with_retry_observability(
                model,
                call_messages,
                context.config,
                span_name=f"gateway.{request.role}.model",
                agent_role=request.role,
                model_name=model_id,
                stage=request.stage,
                attributes={"gateway": True, "logical_operation_id": request.logical_operation_id},
                budget_gate=budget,
            )
            if not isinstance(response, AIMessage):
                raise RuntimeError("sandbox_gateway_provider_returned_non_ai_message")
            return response

        try:
            response = await invoke_with_model_fallback(
                call,
                messages,
                primary_model=primary_model,
                model_fallbacks=configuration.model_fallbacks,
                role=request.role,
                config=context.config,
                on_fallback=lambda event: fallback_events.append(dict(event)),
            )
            usage = self._usage(response)
            outcome = GatewayModelOutcomeV1(
                logical_operation_id=request.logical_operation_id,
                physical_attempt_id=budget.last_physical_attempt_id,
                status="completed",
                message=message_to_dict(response),
                usage=usage,
                fallback_events=fallback_events,
                role=request.role,
                model=selected_model[-1] if selected_model else None,
                provider_ttft_ms=(
                    float(response.response_metadata["provider_ttft_ms"])
                    if response.response_metadata.get("provider_ttft_ms") is not None
                    else None
                ),
            )
            transition = self.internal.signed(
                OperationTransitionRequest,
                run_id=request.run_id,
                fence_token=context.fence_token,
                logical_operation_id=request.logical_operation_id,
                status="completed",
                outcome=outcome.model_dump(mode="json"),
                error_type=None,
            )
            await self.internal.post("/internal/sandbox/operations/transition", transition)
            return outcome
        except Exception as exc:
            kind = classify_model_error(exc, primary_model)
            uncertain = kind in {ModelErrorKind.CANCELLED, ModelErrorKind.UNKNOWN}
            logger.warning(
                "Gateway provider operation failed run_id=%s operation_id=%s "
                "error_type=%s exception_type=%s",
                request.run_id,
                request.logical_operation_id,
                kind.value,
                type(exc).__name__,
            )
            error_detail = " ".join(
                part
                for part in (
                    type(exc).__name__,
                    str(exc)[:200],
                )
                if part
            ).strip()
            outcome = GatewayModelOutcomeV1(
                logical_operation_id=request.logical_operation_id,
                physical_attempt_id=budget.last_physical_attempt_id or "unreserved",
                status="uncertain" if uncertain else "failed",
                usage=self._failure_usage(exc),
                fallback_events=fallback_events,
                role=request.role,
                model=selected_model[-1] if selected_model else None,
                error_type=kind.value,
                error_message=(
                    f"Provider operation failed. {error_detail}"[:400]
                    if error_detail
                    else "Provider operation failed."
                ),
            )
            transition = self.internal.signed(
                OperationTransitionRequest,
                run_id=request.run_id,
                fence_token=context.fence_token,
                logical_operation_id=request.logical_operation_id,
                status="uncertain" if uncertain else "failed",
                outcome=outcome.model_dump(mode="json"),
                error_type=kind.value,
            )
            with suppress(httpx.HTTPError, ValueError, KeyError):
                await self.internal.post("/internal/sandbox/operations/transition", transition)
            return outcome

    async def invoke_model_operation_v2(
        self,
        request: GatewayModelRequestV2,
        context: GatewayRunContext,
    ) -> GatewayModelOutcomeV2:
        """Execute one Wire V2 operation with LiteLLM owning physical recovery."""
        key = (request.run_id, request.logical_operation_id)
        lock = self.operation_locks.setdefault(key, asyncio.Lock())
        async with lock:
            return await self._invoke_model_operation_v2_locked(request, context)

    async def _invoke_model_operation_v2_locked(
        self,
        request: GatewayModelRequestV2,
        context: GatewayRunContext,
    ) -> GatewayModelOutcomeV2:
        # Credential readiness is an admission precondition, not a dispatched
        # model attempt.  Check it before touching the operation journal or
        # reserving budget so a transient registration race remains retryable
        # instead of poisoning the logical operation as uncertain.
        litellm_key = context.api_keys.get("LITELLM_RUN_KEY")
        if not litellm_key:
            raise RuntimeError("sandbox_gateway_run_key_unavailable")
        lookup = self.internal.signed(
            OperationGetRequest,
            run_id=request.run_id,
            fence_token=context.fence_token,
            logical_operation_id=request.logical_operation_id,
            request_digest=hashlib.sha256(request.model_dump_json().encode()).hexdigest(),
        )
        existing = await self.internal.post("/internal/sandbox/operations/get", lookup)
        prior_attempt_count = 0
        if existing.get("found"):
            operation = existing["operation"]
            prior_attempt_count = len(operation.get("physical_attempts") or [])
            raw_outcome = operation.get("outcome")
            if operation.get("status") in {"completed", "failed", "uncertain"} and raw_outcome:
                return GatewayModelOutcomeV2.model_validate(raw_outcome)
            if operation.get("status") == "dispatched":
                return GatewayModelOutcomeV2(
                    logical_operation_id=request.logical_operation_id,
                    status="uncertain",
                    requested_model=request.model,
                    error_code="model_operation_uncertain",
                )

        # Conservative reservation only; settlement uses the provider's measured usage.
        estimated_input = max(1, len(json.dumps(request.messages, ensure_ascii=False).encode("utf-8")))
        estimated_output = max(1, int(request.max_output_tokens or 1024))
        budget = RemoteBudgetGate(
            internal=self.internal,
            run_id=request.run_id,
            task_id=request.task_id,
            fence_token=context.fence_token,
            stage=request.stage,
            logical_operation_id=request.logical_operation_id,
            initial_attempt_count=prior_attempt_count,
        )
        operation_key = f"gateway-v2:{request.logical_operation_id}"
        gateway = self.model_gateways.get(request.run_id)
        if gateway is None:
            gateway = NativeGatewayProvider(api_key=litellm_key)
            self.model_gateways[request.run_id] = gateway
        tools = list(request.tools)
        tool_choice = request.tool_choice
        if request.structured_schema is not None:
            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": STRUCTURED_OUTPUT_TOOL_NAME,
                        "description": "Return the response using the required schema.",
                        "parameters": request.structured_schema,
                        # LiteLLM aliases may span providers whose strict tool
                        # schema subsets differ.  The Worker validates the
                        # forced function result against its Pydantic model and
                        # performs bounded application-level repair.
                        "strict": False,
                    },
                }
            ]
            tool_choice = {
                "type": "function",
                "function": {"name": STRUCTURED_OUTPUT_TOOL_NAME},
            }
        dispatched = False
        try:
            # Reservation and dispatch journaling stay inside the try: any
            # escape path (Worker killed, transient internal 5xx, construction
            # failure) must release the reservation and leave a definite
            # operation state instead of a permanently "dispatched" journal.
            budget.reserve_model_call(
                operation_key,
                estimated_input_tokens=estimated_input,
                estimated_output_tokens=estimated_output,
                model_name=request.model,
                request_digest=hashlib.sha256(request.model_dump_json().encode()).hexdigest(),
            )
            await budget.flush_pending()
            dispatched = True
            outcome = await gateway.complete(request.model_copy(update={"tools": tools, "tool_choice": tool_choice}))
            budget.settle_model_call(
                operation_key,
                input_tokens=outcome.usage.get("input_tokens", 0),
                output_tokens=outcome.usage.get("output_tokens", 0),
                model_name=request.model,
            )
            await budget.flush_pending()
            if request.structured_schema is not None:
                outcome.structured = _wire_structured_args(outcome.message)
            transition = self.internal.signed(
                OperationTransitionRequest,
                run_id=request.run_id,
                fence_token=context.fence_token,
                logical_operation_id=request.logical_operation_id,
                status="completed",
                outcome=outcome.model_dump(mode="json"),
                error_type=None,
            )
            await self.internal.post("/internal/sandbox/operations/transition", transition)
            return outcome
        except asyncio.CancelledError:
            # The Worker (and its budget authority) may vanish mid-dispatch; the
            # server could still have completed and billed the call, so the
            # attempt stays uncertain and the journal must not remain
            # "dispatched" for replays of the same logical operation.
            with suppress(Exception):
                budget.fail_model_call(operation_key, uncertain=True)
                await budget.flush_pending()
            with suppress(httpx.HTTPError, ValueError, KeyError):
                await self.internal.post(
                    "/internal/sandbox/operations/transition",
                    self.internal.signed(
                        OperationTransitionRequest,
                        run_id=request.run_id,
                        fence_token=context.fence_token,
                        logical_operation_id=request.logical_operation_id,
                        status="uncertain",
                        outcome=GatewayModelOutcomeV2(
                            logical_operation_id=request.logical_operation_id,
                            status="uncertain",
                            requested_model=request.model,
                            error_code="model_operation_cancelled",
                        ).model_dump(mode="json"),
                        error_type="model_operation_cancelled",
                    ),
                )
            raise
        except Exception as exc:
            # Once dispatched, only an explicit provider rejection proves that
            # no billable response was produced. Transport/receipt failures do not.
            definite_rejection = isinstance(exc, ModelGatewayError) and exc.code in {
                "invalid_request", "authentication", "model_unavailable",
                "budget_or_rate_limit", "gateway_budget_exceeded",
            }
            uncertain = dispatched and not definite_rejection
            budget.fail_model_call(operation_key, uncertain=uncertain)
            with suppress(httpx.HTTPError, ValueError, KeyError):
                await budget.flush_pending()
            error_code = exc.code if isinstance(exc, ModelGatewayError) else "gateway_model_failed"
            if not dispatched and isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                detail = exc.response.json().get("detail", "")
                if isinstance(detail, str) and detail.startswith("budget_exhausted:"):
                    error_code = detail
            outcome = GatewayModelOutcomeV2(
                logical_operation_id=request.logical_operation_id,
                status="uncertain" if uncertain else "failed",
                requested_model=request.model,
                error_code=error_code,
            )
            transition = self.internal.signed(
                OperationTransitionRequest,
                run_id=request.run_id,
                fence_token=context.fence_token,
                logical_operation_id=request.logical_operation_id,
                status=outcome.status,
                outcome=outcome.model_dump(mode="json"),
                error_type=error_code,
            )
            with suppress(httpx.HTTPError, ValueError, KeyError):
                await self.internal.post("/internal/sandbox/operations/transition", transition)
            return outcome
def create_gateway_app(
    runtime: GatewayRuntime,
    *,
    credential_sweep_seconds: float = GATEWAY_CREDENTIAL_SWEEP_SECONDS,
) -> FastAPI:
    """Create the task-data and trusted-control Gateway application."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        reaper = getattr(runtime, "reap_expired_runs", None)
        if not callable(reaper):
            yield
            return
        task = asyncio.create_task(
            reaper(interval_seconds=credential_sweep_seconds)
        )
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(
        title="InsightForge Sandbox Gateway",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        evict = getattr(runtime, "evict_expired_runs", None)
        if callable(evict):
            evict()
        return {"status": "ok", "registered_runs": len(runtime.runs)}

    @app.post("/internal/v1/runs/register")
    async def register_run(request: GatewayRunRegistrationRequest) -> dict[str, str]:
        try:
            runtime.register(request)
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return {"status": "registered"}

    @app.post("/internal/v1/runs/unregister")
    async def unregister_run(
        request: GatewayRunUnregisterRequest,
    ) -> dict[str, str]:
        try:
            runtime.unregister(request)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"status": "unregistered"}

    @app.post("/v1/models/invoke", response_model=GatewayModelOutcomeV1)
    async def invoke_model(
        request: GatewayModelRequestV1,
        authorization: str = Header(default="", alias="Authorization"),
        timestamp: float = Header(alias="X-Sandbox-Timestamp"),
        nonce: str = Header(alias="X-Sandbox-Nonce"),
        service_signature: str = Header(
            default="",
            alias="X-Sandbox-Service-Signature",
        ),
        fence_token: int | None = Header(
            default=None,
            alias="X-Sandbox-Fence-Token",
        ),
    ) -> GatewayModelOutcomeV1:
        try:
            if authorization.startswith("Bearer "):
                _claims, context = runtime.authorize_task(
                    request,
                    authorization=authorization,
                    timestamp=timestamp,
                    nonce=nonce,
                )
            else:
                if fence_token is None or not service_signature:
                    raise ValueError("sandbox_model_auth_missing")
                context = runtime.authorize_api_model(
                    request,
                    timestamp=timestamp,
                    nonce=nonce,
                    fence_token=fence_token,
                    signature=service_signature,
                )
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return await runtime.invoke_model_operation(request, context)

    @app.post("/v2/models/complete", response_model=GatewayModelOutcomeV2)
    async def complete_model_v2(
        request: GatewayModelRequestV2,
        authorization: str = Header(default="", alias="Authorization"),
        timestamp: float = Header(alias="X-Sandbox-Timestamp"),
        nonce: str = Header(alias="X-Sandbox-Nonce"),
        service_signature: str = Header(
            default="",
            alias="X-Sandbox-Service-Signature",
        ),
        fence_token: int | None = Header(
            default=None,
            alias="X-Sandbox-Fence-Token",
        ),
    ) -> GatewayModelOutcomeV2:
        """Complete one governed Wire V2 model request."""
        try:
            if authorization.startswith("Bearer "):
                _claims, context = runtime.authorize_task(
                    request,
                    authorization=authorization,
                    timestamp=timestamp,
                    nonce=nonce,
                )
            else:
                if fence_token is None or not service_signature:
                    raise ValueError("sandbox_model_auth_missing")
                context = runtime.authorize_api_model(
                    request,
                    timestamp=timestamp,
                    nonce=nonce,
                    fence_token=fence_token,
                    signature=service_signature,
                )
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return await runtime.invoke_model_operation_v2(request, context)

    @app.post("/v1/models/stream")
    async def stream_model(
        request: GatewayModelRequestV1,
        authorization: str = Header(default="", alias="Authorization"),
        timestamp: float = Header(alias="X-Sandbox-Timestamp"),
        nonce: str = Header(alias="X-Sandbox-Nonce"),
        service_signature: str = Header(
            default="",
            alias="X-Sandbox-Service-Signature",
        ),
        fence_token: int | None = Header(
            default=None,
            alias="X-Sandbox-Fence-Token",
        ),
    ) -> StreamingResponse:
        try:
            if authorization.startswith("Bearer "):
                _claims, context = runtime.authorize_task(
                    request,
                    authorization=authorization,
                    timestamp=timestamp,
                    nonce=nonce,
                )
            else:
                if fence_token is None or not service_signature:
                    raise ValueError("sandbox_model_auth_missing")
                context = runtime.authorize_api_model(
                    request,
                    timestamp=timestamp,
                    nonce=nonce,
                    fence_token=fence_token,
                    signature=service_signature,
                )
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

        async def events():
            yield json.dumps({"type": "started"}, separators=(",", ":")) + "\n"
            outcome = await runtime.invoke_model_operation(request, context)
            yield json.dumps(
                {
                    "type": "result",
                    "outcome": outcome.model_dump(mode="json"),
                },
                separators=(",", ":"),
            ) + "\n"

        return StreamingResponse(events(), media_type="application/x-ndjson")

    @app.post(
        "/v1/models/lookup",
        response_model=GatewayOperationLookupOutcomeV1,
    )
    async def lookup_model(
        request: GatewayOperationLookupRequestV1,
        authorization: str = Header(default="", alias="Authorization"),
        timestamp: float = Header(alias="X-Sandbox-Timestamp"),
        nonce: str = Header(alias="X-Sandbox-Nonce"),
        service_signature: str = Header(
            default="",
            alias="X-Sandbox-Service-Signature",
        ),
        fence_token: int | None = Header(
            default=None,
            alias="X-Sandbox-Fence-Token",
        ),
    ) -> GatewayOperationLookupOutcomeV1:
        try:
            if authorization.startswith("Bearer "):
                _claims, context = runtime.authorize_task(
                    request,
                    authorization=authorization,
                    timestamp=timestamp,
                    nonce=nonce,
                )
            else:
                if fence_token is None or not service_signature:
                    raise ValueError("sandbox_model_auth_missing")
                context = runtime.authorize_api_model(
                    request,
                    timestamp=timestamp,
                    nonce=nonce,
                    fence_token=fence_token,
                    signature=service_signature,
                )
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return await runtime.lookup_model_operation(request, context)

    @app.post(
        "/v1/tools/catalog",
        response_model=GatewayToolCatalogOutcomeV1,
    )
    @native_tools_scope
    async def tool_catalog(
        request: GatewayToolCatalogRequestV1,
        authorization: str = Header(default="", alias="Authorization"),
        timestamp: float = Header(alias="X-Sandbox-Timestamp"),
        nonce: str = Header(alias="X-Sandbox-Nonce"),
    ) -> GatewayToolCatalogOutcomeV1:
        try:
            _claims, context = runtime.authorize_task(
                request,
                authorization=authorization,
                timestamp=timestamp,
                nonce=nonce,
            )
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return await runtime.tool_catalog(request, context)

    @app.post("/v1/team")
    async def team_input(
        request: TeamWorkerRequest,
        authorization: str = Header(default="", alias="Authorization"),
        timestamp: float = Header(alias="X-Sandbox-Timestamp"),
        nonce: str = Header(alias="X-Sandbox-Nonce"),
    ) -> dict[str, Any]:
        try:
            _claims, context = runtime.authorize_task(
                request, authorization=authorization, timestamp=timestamp, nonce=nonce,
            )
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        if request.action not in {"input", "checkpoint"}:
            raise HTTPException(status_code=400, detail="unsupported_worker_team_action")
        from open_deep_research.sandbox.internal_api import TeamBridgeRequest
        return await runtime.internal.post("/internal/sandbox/team", runtime.internal.signed(
            TeamBridgeRequest, run_id=request.run_id, task_id=request.task_id,
            fence_token=context.fence_token, action=request.action, payload=request.payload,
        ))

    @app.post("/v1/tools/call", response_model=GatewayToolOutcomeV1)
    @native_tools_scope
    async def invoke_tool(
        request: GatewayToolRequestV1,
        authorization: str = Header(default="", alias="Authorization"),
        timestamp: float = Header(alias="X-Sandbox-Timestamp"),
        nonce: str = Header(alias="X-Sandbox-Nonce"),
    ) -> GatewayToolOutcomeV1:
        try:
            _claims, context = runtime.authorize_task(
                request,
                authorization=authorization,
                timestamp=timestamp,
                nonce=nonce,
            )
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return await runtime.invoke_tool(request, context)

    @app.post("/v1/tools/authorize-local", response_model=GatewayToolOutcomeV1)
    @native_tools_scope
    async def authorize_local_tool(
        request: GatewayToolRequestV1,
        authorization: str = Header(default="", alias="Authorization"),
        timestamp: float = Header(alias="X-Sandbox-Timestamp"),
        nonce: str = Header(alias="X-Sandbox-Nonce"),
    ) -> GatewayToolOutcomeV1:
        try:
            _claims, context = runtime.authorize_task(
                request,
                authorization=authorization,
                timestamp=timestamp,
                nonce=nonce,
            )
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return await runtime.authorize_local_tool(request, context)

    return app


def main() -> None:
    """Run the Gateway service on the internal task network."""
    import asyncio

    import uvicorn

    configurable = Configuration.from_runnable_config(None)
    if not configurable.sandbox_enabled:
        raise SystemExit("SANDBOX_ENABLED must be true for sandbox-gateway")
    if os.getenv("SANDBOX_GATEWAY_FAKE_PROVIDER", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    } and os.getenv("APP_ENV", "production").lower() not in {
        "development",
        "test",
    }:
        raise SystemExit(
            "SANDBOX_GATEWAY_FAKE_PROVIDER is restricted to development/test"
        )
    runtime = GatewayRuntime(configurable)

    async def serve() -> None:
        from open_deep_research.sandbox.egress_proxy import GatewayEgressProxy

        proxy = GatewayEgressProxy(runtime)
        proxy_server = await asyncio.start_server(
            proxy.handle,
            "0.0.0.0",
            int(os.getenv("SANDBOX_GATEWAY_PROXY_PORT", "8080")),
        )
        server = uvicorn.Server(
            uvicorn.Config(
                create_gateway_app(runtime),
                host="0.0.0.0",
                port=int(os.getenv("SANDBOX_GATEWAY_PORT", "8081")),
                log_level="info",
            )
        )
        async with proxy_server:
            await asyncio.gather(proxy_server.serve_forever(), server.serve())

    asyncio.run(serve())


if __name__ == "__main__":
    main()
