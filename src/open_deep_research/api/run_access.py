"""HTTP run configuration, ownership and fenced sandbox context."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException

from open_deep_research.api.contracts import RunRequest
from open_deep_research.api.run_admission import _user_identity
from open_deep_research.api.run_recovery import _load_manifests
from open_deep_research.api.run_registry import RunRecord
from open_deep_research.api.run_retention import _TERMINAL_RUN_STATUSES
from open_deep_research.configuration import Configuration
from open_deep_research.documents.database import document_schema_available
from open_deep_research.documents.repository import DocumentConflictError, validate_selection
from open_deep_research.events.public import RunEventStore
from open_deep_research.events.task_activity import TaskActivityStore, activity_summary
from open_deep_research.logging_config import current_request_id
from open_deep_research.report.models import PublisherTheme
from open_deep_research.run_context import JournalCorruptedError, RunContextStore
from open_deep_research.sandbox.approvals import SecurityApprovalStore
from open_deep_research.sandbox.internal_api import InternalRunContext
from open_deep_research.security.inputs import validate_http_configurable, validate_http_metadata
from security.rbac import Principal, apply_principal_to_config

logger = logging.getLogger(__name__)


def _config_from_request(request: RunRequest, principal: Principal) -> dict[str, Any]:
    try:
        validate_http_configurable(request.configurable)
        validate_http_metadata(request.metadata)
    except ValueError as exc:
        logger.warning("security.unsafe_config_rejected: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    config = {
        "configurable": dict(request.configurable),
        "metadata": {
            **request.metadata,
            "source_selection": request.source_selection.model_dump(mode="json"),
            "publication_theme": (
                request.publication_theme or PublisherTheme()
            ).model_dump(mode="json"),
            "deployment_surface": "http",
            "request_id": current_request_id(),
        },
    }
    return apply_principal_to_config(config, principal)


async def _validate_run_sources(request: RunRequest, principal: Principal) -> list[Any]:
    """Enforce feature health, ownership and ready state before creating a Run."""
    selection = request.source_selection
    if selection.documents_enabled and not document_schema_available():
        raise HTTPException(status_code=503, detail="document_research_unavailable")
    try:
        return await validate_selection(principal.user_id, selection)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="document_not_found") from exc
    except DocumentConflictError as exc:
        raise HTTPException(status_code=409, detail="document_not_ready") from exc


def _find_idempotent_run(
    config: dict[str, Any],
    owner_id: str | None,
    idempotency_key: str,
) -> Any | None:
    return next(
        (
            manifest
            for manifest in _load_manifests(config)
            if manifest.owner_id == owner_id and manifest.idempotency_key == idempotency_key
        ),
        None,
    )


def _require_record_owner(record: RunRecord, user: Principal) -> None:
    """Hide in-memory runs from users other than their owner."""
    metadata = getattr(record.engine, "config", {}).get("metadata", {})
    owner = metadata.get("owner") or metadata.get("user_id")
    if not owner or str(owner) != _user_identity(user):
        raise HTTPException(status_code=404, detail="Run not found")


def _augment_run_projection(
    run_id: str,
    projection: Any,
    configurable: Configuration,
) -> Any:
    """Attach task activity summaries without changing the public event reducer."""
    if projection is None:
        return None
    if projection.status in _TERMINAL_RUN_STATUSES:
        try:
            SecurityApprovalStore(
                run_id,
                runs_dir=configurable.runs_dir,
            ).deny_pending(actor="system", reason="run_terminal")
        except (OSError, RuntimeError, ValueError):
            pass
        projection.pending_security_approvals = []
    else:
        try:
            _version, approvals = SecurityApprovalStore(
                run_id,
                runs_dir=configurable.runs_dir,
            ).list(status="pending")
            projection.pending_security_approvals = [
                {
                    "approval_id": item.approval_id,
                    "task_id": item.task_id,
                    "kind": item.kind,
                    "capability": item.capability,
                    "target": item.target,
                    "status": item.status,
                    "expires_at": item.expires_at,
                }
                for item in approvals
            ]
        except (OSError, RuntimeError, ValueError):
            pass
    for task_id, task in projection.task_items.items():
        try:
            store = TaskActivityStore(run_id, task_id, runs_dir=configurable.runs_dir)
            if store.exists:
                task.update(activity_summary(store.read()))
        except (OSError, RuntimeError, ValueError):
            task.setdefault("activity_available", False)
    return projection


class RunAccess:
    """Resolve ownership and sandbox authority using the active host services."""

    def __init__(
        self,
        *,
        lookup_record: Callable[[str], RunRecord | None],
        native_service: Callable[[], Any],
    ) -> None:
        self.lookup_record = lookup_record
        self.native_service = native_service

    async def _rbac_run_owner_checker(self, _db, principal, run_id: str) -> bool:
        """Ownership bridge used by ``require_run_owner`` (prepared for cutover)."""
        if self.native_service() is not None:
            try:
                await self.native_service().store.load(run_id, principal.user_id)
                return True
            except KeyError:
                pass
        record = self.lookup_record(run_id)
        if record is not None:
            metadata = getattr(record.engine, "config", {}).get("metadata", {})
            owner = metadata.get("owner") or metadata.get("user_id")
            return bool(owner) and str(owner) == principal.user_id
        configurable = Configuration.from_runnable_config(None)
        try:
            manifest = RunContextStore(run_id, runs_dir=configurable.runs_dir).load_manifest()
        except (ValueError, JournalCorruptedError, OSError):
            return False
        return bool(manifest.owner_id) and manifest.owner_id == principal.user_id

    async def _rbac_task_owner_checker(self, _db, principal, key: tuple[str, str]) -> bool:
        """Ownership bridge used by ``require_task_owner`` (prepared for cutover)."""
        run_id, task_id = key
        if not await self._rbac_run_owner_checker(_db, principal, run_id):
            return False
        configurable = Configuration.from_runnable_config(None)
        projection = RunEventStore(run_id, runs_dir=configurable.runs_dir).project()
        return task_id in projection.task_items

    def _require_run_owner(self, run_id: str, user: Principal) -> tuple[RunRecord | None, Configuration]:
        """Authorize an active or persisted run and return its effective config."""
        record = self.lookup_record(run_id)
        if record is not None:
            _require_record_owner(record, user)
            return record, Configuration.from_runnable_config(getattr(record.engine, "config", None))
        configurable = Configuration.from_runnable_config(None)
        try:
            manifest = RunContextStore(run_id, runs_dir=configurable.runs_dir).load_manifest()
        except (ValueError, JournalCorruptedError, OSError):
            raise HTTPException(status_code=404, detail="Run not found") from None
        if not manifest.owner_id or manifest.owner_id != _user_identity(user):
            raise HTTPException(status_code=404, detail="Run not found")
        return None, Configuration.from_runnable_config(manifest.config)

    def _resolve_internal_sandbox_run(self, run_id: str) -> InternalRunContext | None:
        """Resolve the live, fenced API authority for trusted sandbox services."""
        record = self.lookup_record(run_id)
        if record is None or record.engine.run_fence_token is None:
            return None
        config = record.engine.config
        return InternalRunContext(
            config=config,
            configurable=Configuration.from_runnable_config(config),
            fence_token=int(record.engine.run_fence_token),
            started_at=float(record.engine.started_at),
        )

    async def _native_key_cleanup(self, run_id, manager):
        if self.native_service() is None:
            return False
        from open_deep_research.agentscope_runtime.native_security import cleanup_run_key
        return await cleanup_run_key(self.native_service().store, run_id, manager)

    async def _native_sandbox_ledger(self, run_id: str):
        """原生运行的 SQL 账本权威；仅网关计账的活跃运行返回账本。

        旧引擎运行与本缝未启用时返回 None，内部预算端点保持文件账本行为。
        """
        service = self.native_service()
        if service is None:
            return None
        return await service.pipeline_factory.gateway_ledger(run_id)

    async def _sandbox_store_context(
        self,
        run_id: str,
        *,
        require_live_fence: bool = False,
    ) -> tuple[Configuration, int, dict[str, Any]]:
        """Resolve store configuration and current fence after RBAC authorization."""
        if self.native_service() is not None:
            from open_deep_research.agentscope_runtime.native_security import sandbox_context
            native = await sandbox_context(self.native_service(), run_id, require_live_fence=require_live_fence)
            if native is not None:
                return native
        record = self.lookup_record(run_id)
        if record is not None:
            if require_live_fence and record.status in _TERMINAL_RUN_STATUSES:
                raise HTTPException(status_code=409, detail="stale_fence")
            token = record.engine.run_fence_token
            if token is None:
                raise HTTPException(status_code=409, detail="run_not_active")
            return (
                Configuration.from_runnable_config(record.engine.config),
                int(token),
                record.engine.config,
            )
        if require_live_fence:
            raise HTTPException(status_code=409, detail="stale_fence")
        configurable = Configuration.from_runnable_config(None)
        try:
            manifest = RunContextStore(run_id, runs_dir=configurable.runs_dir).load_manifest()
        except Exception as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc
        if not manifest.fence_token:
            raise HTTPException(status_code=409, detail="run_has_no_fence")
        config = {
            "configurable": {"runs_dir": configurable.runs_dir},
            "metadata": {"run_id": run_id, "run_fence_token": manifest.fence_token},
        }
        return configurable, int(manifest.fence_token), config
