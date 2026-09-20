"""Security approval and egress decisions with host-owned live fence resolution."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Literal
from fastapi import APIRouter, Depends, HTTPException
from open_deep_research.api.contracts import (
    EgressModeChangeRequest,
    EgressTargetDecisionRequest,
    SecurityApprovalDecisionRequest,
)
from open_deep_research.configuration import Configuration
from open_deep_research.events.public import event_publisher_from_config
from open_deep_research.run_context import JournalCorruptedError
from open_deep_research.sandbox.approvals import SecurityApprovalStore
from open_deep_research.sandbox.egress_ledger_store import (
    EgressClassificationStore,
    RunEgressModeStore,
)
from open_deep_research.sandbox.egress_mode import (
    RUN_EGRESS_MODE_VALUES,
    effective_egress_mode,
    policy_baseline_mode,
)
from open_deep_research.sandbox.schema import network_target_decision, resolve_profile
from open_deep_research.tasks.registry import TaskStatus, get_task_registry
from security.rbac import Principal, require_run_owner_or_any
from security.rbac.permissions import (
    RESEARCH_SECURITY_APPROVAL_READ_OWN,
    RESEARCH_SECURITY_APPROVAL_READ_ANY,
    RESEARCH_SECURITY_APPROVAL_RESOLVE_OWN,
    RESEARCH_SECURITY_APPROVAL_RESOLVE_ANY,
    RESEARCH_RUN_INTERACT_OWN,
)


def _egress_mode_state(
    configurable: Configuration,
    *,
    run_id: str,
    fence_token: int,
    override_mode: str | None,
) -> dict[str, Any]:
    """Compose baseline, run setting, override, and effective egress mode."""
    try:
        _bundle, _profile_id, profile = resolve_profile(configurable)
    except Exception as exc:
        raise HTTPException(
            status_code=409, detail="sandbox_policy_unavailable"
        ) from exc
    baseline = policy_baseline_mode(profile.network)
    run_setting = getattr(configurable, "sandbox_egress_approval_mode", "profile")
    effective = effective_egress_mode(baseline, run_setting, override_mode)
    return {
        "run_id": run_id,
        "fence_token": fence_token,
        "baseline_mode": baseline,
        "run_setting": run_setting,
        "override": override_mode,
        "effective_mode": effective.mode,
        "capped": effective.capped,
    }


def _runtime_egress_override(
    run_id: str,
    runs_dir: str,
    *,
    fence_token: int,
) -> str | None:
    """Read the stored override; stale-fence overrides read as absent."""
    override = RunEgressModeStore(run_id, runs_dir=runs_dir).get()
    if override is None or override.fence_token != fence_token:
        return None
    return override.mode


def _read_egress_state(
    run_id: str, configurable: Configuration, fence_token: int
) -> dict[str, Any]:
    mode = _egress_mode_state(
        configurable,
        run_id=run_id,
        fence_token=fence_token,
        override_mode=_runtime_egress_override(
            run_id, configurable.runs_dir, fence_token=fence_token
        ),
    )
    store = SecurityApprovalStore(run_id, runs_dir=configurable.runs_dir)
    target_state = store.target_state(fence_token)
    _, approvals = store.list()
    ledger = EgressClassificationStore(run_id, runs_dir=configurable.runs_dir)
    try:
        classifications = list(ledger.load().values())
        health = ledger.classifier_state()
    except OSError, ValueError, JournalCorruptedError:
        # Manual decisions remain available when the auxiliary classifier
        # ledger is unreadable. Do not invent zero consumed model budget.
        classifications = []
        health = {"degraded": True, "reason": "state_unavailable"}
    _, _, profile = resolve_profile(configurable)
    for item in target_state["targets"]:
        host, port = item["target"]["domain"], item["target"]["port"]
        item["policy_denied"] = (
            network_target_decision(profile.network, host, port) == "deny"
        )
        matching = [
            entry
            for entry in classifications
            if entry.get("host") == host
            and entry.get("port") == port
            and entry.get("capability") == item["capability"]
            and str(entry.get("fingerprint", "")).startswith("egress:v2:")
        ]
        item["classification"] = max(
            matching, key=lambda e: e.get("classified_at", 0), default=None
        )
    limit = configurable.egress_classifier_max_calls_per_run
    health.update(max_calls=limit)
    if health.get("reason") != "state_unavailable":
        health["remaining_calls"] = max(0, limit - int(health.get("calls_used", 0)))
    allowed_modes = [
        candidate
        for candidate in ("manual", "auto", "open")
        if (
            resolved := effective_egress_mode(
                mode["baseline_mode"], mode["run_setting"], candidate
            )
        ).mode
        == candidate
        and not resolved.capped
    ]
    return {
        **mode,
        **target_state,
        "allowed_modes": allowed_modes,
        "health": health,
        "records": [
            item.model_dump(mode="json")
            for item in approvals
            if item.fence_token == fence_token
        ],
        "classifications": classifications[-200:],
    }


class SecurityRoutes:
    """Keep authorization and fence resolution bound to the active application."""

    def __init__(self, *, sandbox_store_context, run_owner_checker):
        self._sandbox_store_context = sandbox_store_context
        self._rbac_run_owner_checker = run_owner_checker
        self.router = APIRouter(tags=["security"])
        self.router.add_api_route(
            "/runs/{run_id}/security-approvals",
            self.list_security_approvals,
            methods=["GET"],
        )
        self.router.add_api_route(
            "/runs/{run_id}/security-approvals/{approval_id}",
            self.resolve_security_approval,
            methods=["POST"],
        )
        self.router.add_api_route(
            "/runs/{run_id}/egress-state", self.get_egress_state, methods=["GET"]
        )
        self.router.add_api_route(
            "/runs/{run_id}/egress-targets/{target_id}/decision",
            self.decide_egress_target,
            methods=["POST"],
        )
        self.router.add_api_route(
            "/runs/{run_id}/egress-mode", self.get_run_egress_mode, methods=["GET"]
        )
        self.router.add_api_route(
            "/runs/{run_id}/egress-mode", self.set_run_egress_mode, methods=["POST"]
        )

    async def list_security_approvals(
        self,
        run_id: str,
        status: Literal["pending", "resolved", "expired", "consumed"]
        | None = "pending",
        user: Principal = Depends(
            require_run_owner_or_any(
                RESEARCH_SECURITY_APPROVAL_READ_OWN.code,
                RESEARCH_SECURITY_APPROVAL_READ_ANY.code,
            )
        ),
    ) -> dict[str, Any]:
        """List the caller-authorized run's durable sandbox approval queue."""
        del user
        configurable, _fence_token, _config = await self._sandbox_store_context(run_id)
        version, approvals = await asyncio.to_thread(
            SecurityApprovalStore(run_id, runs_dir=configurable.runs_dir).list,
            status=status,
        )
        return {
            "run_id": run_id,
            "version": version,
            "approvals": [approval.model_dump(mode="json") for approval in approvals],
        }

    async def resolve_security_approval(
        self,
        run_id: str,
        approval_id: str,
        request: SecurityApprovalDecisionRequest,
        user: Principal = Depends(
            require_run_owner_or_any(
                RESEARCH_SECURITY_APPROVAL_RESOLVE_OWN.code,
                RESEARCH_SECURITY_APPROVAL_RESOLVE_ANY.code,
            )
        ),
    ) -> dict[str, Any]:
        """Resolve one approval for exactly the live run ownership epoch."""
        configurable, fence_token, config = await self._sandbox_store_context(
            run_id,
            require_live_fence=True,
        )
        try:
            approval = await asyncio.to_thread(
                SecurityApprovalStore(run_id, runs_dir=configurable.runs_dir).resolve,
                approval_id,
                decision=request.decision,
                actor=user.user_id,
                reason=request.reason,
                expected_fence_token=fence_token,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="security_approval_not_found"
            ) from exc
        except ValueError as exc:
            status_code = 409 if str(exc) == "stale_fence" else 400
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        await event_publisher_from_config(config).publish(
            "security.approval.resolved",
            stage="researching",
            payload={
                "approval_id": approval.approval_id,
                "task_id": approval.task_id,
                "kind": approval.kind,
                "capability": approval.capability,
                "decision": approval.decision,
                "status": approval.status,
                "version": approval.version,
            },
            dedupe_key=f"security-approval:{approval.approval_id}:resolved:{approval.version}",
        )
        task = get_task_registry().get(approval.task_id)
        if task is not None and task.run_id == run_id:
            _version, pending = await asyncio.to_thread(
                SecurityApprovalStore(run_id, runs_dir=configurable.runs_dir).list,
                status="pending",
            )
            task_pending = [
                item for item in pending if item.task_id == approval.task_id
            ]
            if task_pending:
                task.pending_domain = (
                    str(task_pending[0].target.get("domain") or "") or None
                )
                task.pending_domain_tool = task_pending[0].capability
            else:
                task.pending_domain = None
                task.pending_domain_tool = None
            if not task_pending and task.status == TaskStatus.WAITING_FOR_CONFIRMATION:
                get_task_registry().update_status(approval.task_id, TaskStatus.RUNNING)
        return approval.model_dump(mode="json")

    async def get_egress_state(
        self,
        run_id: str,
        user: Principal = Depends(
            require_run_owner_or_any(
                RESEARCH_SECURITY_APPROVAL_READ_OWN.code,
                RESEARCH_SECURITY_APPROVAL_READ_ANY.code,
            )
        ),
    ) -> dict[str, Any]:
        """Return a reloadable snapshot of permissions, decisions, and health."""
        configurable, fence_token, _ = await self._sandbox_store_context(run_id)
        result = await asyncio.to_thread(
            _read_egress_state, run_id, configurable, fence_token
        )
        owns_run = await self._rbac_run_owner_checker(None, user, run_id)
        result["can_resolve"] = user.has_any(
            [RESEARCH_SECURITY_APPROVAL_RESOLVE_ANY.code]
        ) or (owns_run and user.has_any([RESEARCH_SECURITY_APPROVAL_RESOLVE_OWN.code]))
        result["can_interact"] = owns_run and user.has_any(
            [RESEARCH_RUN_INTERACT_OWN.code]
        )
        return result

    async def decide_egress_target(
        self,
        run_id: str,
        target_id: str,
        request: EgressTargetDecisionRequest,
        user: Principal = Depends(
            require_run_owner_or_any(
                RESEARCH_SECURITY_APPROVAL_RESOLVE_OWN.code,
                RESEARCH_SECURITY_APPROVAL_RESOLVE_ANY.code,
            )
        ),
    ) -> dict[str, Any]:
        """Apply an exact target override, never overriding administrator denial."""
        configurable, fence_token, config = await self._sandbox_store_context(
            run_id, require_live_fence=True
        )
        store = SecurityApprovalStore(run_id, runs_dir=configurable.runs_dir)
        snapshot = await asyncio.to_thread(store.target_state, fence_token)
        target = next(
            (item for item in snapshot["targets"] if item["target_id"] == target_id),
            None,
        )
        if target is None:
            raise HTTPException(status_code=404, detail="egress_target_not_found")
        _, _, profile = resolve_profile(configurable)
        if (
            request.decision == "allow_run"
            and network_target_decision(
                profile.network, target["target"]["domain"], target["target"]["port"]
            )
            == "deny"
        ):
            raise HTTPException(status_code=403, detail="egress_target_policy_denied")
        try:
            result = await asyncio.to_thread(
                store.decide_target,
                target_id,
                decision=request.decision,
                reason=request.reason,
                actor=user.user_id,
                expected_version=request.expected_version,
                fence_token=fence_token,
            )
        except ValueError as exc:
            latest = await asyncio.to_thread(store.target_state, fence_token)
            raise HTTPException(
                status_code=409, detail={"code": str(exc), "state": latest}
            ) from exc
        await event_publisher_from_config(config).publish(
            "security.egress_target_changed",
            stage="researching",
            payload=result,
            dedupe_key=f"egress-target:{target_id}:{fence_token}:{result['version']}",
        )
        return result

    async def get_run_egress_mode(
        self,
        run_id: str,
        user: Principal = Depends(
            require_run_owner_or_any(
                RESEARCH_SECURITY_APPROVAL_READ_OWN.code,
                RESEARCH_SECURITY_APPROVAL_READ_ANY.code,
            )
        ),
    ) -> dict[str, Any]:
        """Report the run's current egress approval mode and its provenance."""
        del user
        configurable, fence_token, _config = await self._sandbox_store_context(run_id)
        override_mode = _runtime_egress_override(
            run_id,
            configurable.runs_dir,
            fence_token=fence_token,
        )
        return _egress_mode_state(
            configurable,
            run_id=run_id,
            fence_token=fence_token,
            override_mode=override_mode,
        )

    async def set_run_egress_mode(
        self,
        run_id: str,
        request: EgressModeChangeRequest,
        user: Principal = Depends(
            require_run_owner_or_any(
                RESEARCH_SECURITY_APPROVAL_RESOLVE_OWN.code,
                RESEARCH_SECURITY_APPROVAL_RESOLVE_ANY.code,
            )
        ),
    ) -> dict[str, Any]:
        """Switch the runtime egress mode; widening past the baseline is refused."""
        if request.mode not in RUN_EGRESS_MODE_VALUES:
            raise HTTPException(status_code=400, detail="sandbox_egress_mode_invalid")
        configurable, fence_token, config = await self._sandbox_store_context(
            run_id,
            require_live_fence=True,
        )
        previous_override = _runtime_egress_override(
            run_id,
            configurable.runs_dir,
            fence_token=fence_token,
        )
        previous_state = _egress_mode_state(
            configurable,
            run_id=run_id,
            fence_token=fence_token,
            override_mode=previous_override,
        )
        candidate = _egress_mode_state(
            configurable,
            run_id=run_id,
            fence_token=fence_token,
            override_mode=request.mode,
        )
        if candidate["capped"]:
            raise HTTPException(
                status_code=409,
                detail=(
                    "sandbox_egress_mode_capped_by_baseline:"
                    f"{candidate['baseline_mode']}"
                ),
            )
        await asyncio.to_thread(
            RunEgressModeStore(run_id, runs_dir=configurable.runs_dir).set,
            mode=request.mode,
            actor=user.user_id,
            fence_token=fence_token,
            origin="api",
        )
        await event_publisher_from_config(config).publish(
            "security.egress_mode_changed",
            stage="researching",
            payload={
                "mode": request.mode,
                "effective_mode": candidate["effective_mode"],
                "version": time.time_ns(),
                "previous_mode": previous_state["effective_mode"],
                "actor": user.user_id,
                "origin": "api",
            },
            dedupe_key=f"security-egress-mode:{run_id}:{request.mode}:{time.time_ns()}",
        )
        return candidate
