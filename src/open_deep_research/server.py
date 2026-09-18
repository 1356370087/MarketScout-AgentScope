"""FastAPI service entrypoint for the LangGraph-free runtime."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import html
import json
import logging
import os
import re
import shutil
import sqlite3
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import portalocker
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from open_deep_research.agents.query_engine import QueryEngine
from open_deep_research.api.projections import _stable_output, _stable_report_review  # noqa: F401
from open_deep_research.api import streams
from open_deep_research.api.contracts import (
    EgressModeChangeRequest,
    EgressTargetDecisionRequest,
    HumanActionRequest,
    HumanFeedbackRequest,
    PublicationRequest,
    ResumeRunRequest,
    RunRequest,
    SecurityApprovalDecisionRequest,
    TeamMessageRequest,
)
from open_deep_research.api.streams import (
    StreamOptions,
    _sse_headers,
)
from open_deep_research.api_governance import ConnectionLimiter, FixedWindowRateLimiter
from open_deep_research.budgets import RunBudgetLedger
from open_deep_research.configuration import Configuration
from open_deep_research.documents.database import (
    close_document_pool,
    document_health,
    document_operational_snapshot,
    document_schema_available,
    initialize_document_schema,
)
from open_deep_research.documents.embeddings import close_embedding_clients
from open_deep_research.documents.repository import (
    DocumentConflictError,
    bind_run_sources,
    release_run_sources,
    validate_selection,
)
from open_deep_research.documents.router import router as documents_router
from open_deep_research.documents.settings import get_document_settings
from open_deep_research.events.public import (
    PUBLIC_EVENT_SCHEMA_VERSION,
    RunEventStore,
    event_publisher_from_config,
)
from open_deep_research.events.publications import (
    PUBLICATION_EVENT_SCHEMA_VERSION,
    PublicationEventStore,
    publication_event_payload,
)
from open_deep_research.events.task_activity import (
    PUBLIC_TASK_ACTIVITY_SCHEMA_VERSION,
    TaskActivityStore,
    activity_summary,
    derive_trace_activity,
)
from open_deep_research.knowledge.batch_router import router as batch_router
from open_deep_research.knowledge.fact_wiki_router import router as fact_wiki_router
from open_deep_research.knowledge.health_router import router as health_router
from open_deep_research.knowledge.router import router as knowledge_router
from open_deep_research.knowledge.search_router import router as knowledge_search_router
from open_deep_research.knowledge.workspace_router import router as workspace_router
from open_deep_research.logging_config import (
    bind_request_id,
    configure_logging,
    current_request_id,
)
from open_deep_research.models.catalog import (
    LiteLLMModelCatalogClient,
    ModelCatalogError,
)
from open_deep_research.models.circuit import get_model_circuit_registry
from open_deep_research.models.credentials import (
    LiteLLMKeyAdminClient,
    LiteLLMKeyConfigurationError,
    RunKeyManager,
    RunKeySecretStore,
    RunKeySettings,
)
from open_deep_research.models.key_reconciler import run_key_reconciler_loop
from open_deep_research.models.spend import (
    LiteLLMSpendClient,
    aggregate_spend_logs_by_tag_prefix,
    run_spend_index,
)
from open_deep_research.observability import SQLiteTraceStore, get_trace_recorder
from open_deep_research.observability.telemetry import get_prometheus_metrics
from open_deep_research.report.models import PublisherTheme
from open_deep_research.report.publication_store import (
    PUBLICATION_SCHEMA_VERSION,
    PublicationJob,
    PublicationJobStore,
    get_publisher_settings,
    worker_available,
)
from open_deep_research.report.publishers import PUBLICATION_FORMATS
from open_deep_research.run_context import (
    JournalCorruptedError,
    RunContextError,
    RunContextStore,
)
from open_deep_research.run_control import RunControlStore
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
from open_deep_research.sandbox.internal_api import (
    InternalRunContext,
    build_internal_sandbox_router,
    reconcile_run_gateway_usage,
)
from open_deep_research.sandbox.schema import network_target_decision, resolve_profile
from open_deep_research.security.inputs import (
    validate_http_configurable,
    validate_http_metadata,
)
from open_deep_research.tasks.lease import LeaderLeaseManager, LeaseConflictError
from open_deep_research.tasks.registry import TaskStatus, get_task_registry
from security.rbac import (
    Principal,
    apply_principal_to_config,
    check_database_connection,
    mount_rbac,
    register_ownership_checker,
    require_active_user,
    require_permissions,
    require_run_owner_or_any,
    shutdown_rbac,
    startup_checks,
)
from security.rbac.app_extension import StartupError, assert_schema_current
from security.rbac.database import session_scope
from security.rbac.dependencies import reauthorize_session
from security.rbac.permissions import (
    RESEARCH_DIAGNOSTICS_PREVIEW,
    RESEARCH_OBSERVABILITY_READ_OWN,
    RESEARCH_RUN_CONTROL_OWN,
    RESEARCH_RUN_CREATE,
    RESEARCH_RUN_INTERACT_OWN,
    RESEARCH_RUN_READ_OWN,
    RESEARCH_SECURITY_APPROVAL_READ_ANY,
    RESEARCH_SECURITY_APPROVAL_READ_OWN,
    RESEARCH_SECURITY_APPROVAL_RESOLVE_ANY,
    RESEARCH_SECURITY_APPROVAL_RESOLVE_OWN,
    RESEARCH_TASK_ACTIVITY_READ_OWN,
)
from security.rbac.settings import get_settings as get_iam_settings
from security.rbac.settings import local_dev_bypass_enabled

load_dotenv()
configure_logging()
















@dataclass
class RunRecord:
    """In-memory HTTP run state."""

    run_id: str
    engine: QueryEngine
    status: str = "pending"
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=200))
    result: dict[str, Any] | None = None
    task: asyncio.Task | None = None
    finished_at: float | None = None


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Run startup recovery and gracefully interrupt live work on shutdown."""
    global _retention_sweep_task, _run_key_reconciler_task
    _shutting_down.clear()
    _sse_shutdown.clear()
    await startup_checks()
    global _native_research_service
    from open_deep_research.agentscope_runtime.native_host import (
        native_engine_enabled,
    )

    if native_engine_enabled():
        from open_deep_research.agentscope_runtime.native_host import (
            build_native_research_service,
            mount_native_research,
        )

        _native_research_service = await build_native_research_service(
            runs_dir=Configuration.from_runnable_config(None).runs_dir,
            database_url=os.getenv("AS_RECOVERY_DATABASE_URL") or None,
        )
        mount_native_research(app, _native_research_service)
        logger.info(
            "RESEARCH_ENGINE=native: research run routes served by the native "
            "runtime; legacy runs are read-only history"
        )
    document_schema_error: str | None = None
    if get_document_settings().enabled:
        try:
            await assert_schema_current("0016_research_teams")
        except StartupError as exc:
            if not str(exc).startswith("schema_revision_mismatch:"):
                raise
            document_schema_error = await initialize_document_schema(str(exc))
        else:
            document_schema_error = await initialize_document_schema()
    else:
        try:
            await assert_schema_current("0016_research_teams")
        except StartupError as exc:
            expected_old_revision = (
                "schema_revision_mismatch:got=0012_facts:"
                "expected=0016_research_teams"
            )
            if str(exc) != expected_old_revision:
                raise
    if document_schema_error:
        logger.error(
            "Document research disabled by startup schema probe: %s",
            document_schema_error,
        )
    configurable = Configuration.from_runnable_config(None)
    if configurable.sandbox_enabled:
        from open_deep_research.sandbox.controller_client import (
            SandboxControllerClient,
        )
        from open_deep_research.sandbox.doctor import diagnose
        from open_deep_research.sandbox.schema import load_policy_bundle

        startup_grace = min(
            120.0,
            max(0.0, float(os.getenv("SANDBOX_STARTUP_GRACE_SECONDS", "30"))),
        )
        startup_deadline = time.monotonic() + startup_grace
        while True:
            sandbox_report = await asyncio.to_thread(diagnose)
            if sandbox_report.get("ready"):
                break
            if time.monotonic() >= startup_deadline:
                raise RuntimeError(
                    "sandbox_unavailable:"
                    + ",".join(
                        str(item)
                        for item in sandbox_report.get("failures", [])
                    )
                )
            await asyncio.sleep(1)
        # A fresh API process owns no live task leases yet. Stop every Worker
        # from this deployment before recovery can schedule replacements; the
        # Controller label scope prevents touching unrelated Docker resources.
        bundle = load_policy_bundle(configurable.sandbox_policy_path)
        await SandboxControllerClient(configurable, bundle).reconcile_tasks([])
    if configurable.run_recovery_sweep_on_startup:
        try:
            await _run_recovery_sweep(configurable)
        except Exception as exc:  # noqa: BLE001 - recovery is fail-open
            logger.warning("run recovery sweep failed: %s", exc)
    if configurable.retention_sweep_interval_seconds > 0:
        _retention_sweep_task = asyncio.create_task(
            _retention_sweep_loop(configurable)
        )
    if configurable.model_backend == "litellm":
        key_settings = RunKeySettings.from_env()
        _run_key_reconciler_task = asyncio.create_task(
            run_key_reconciler_loop(
                key_settings,
                native_cleanup=_native_key_cleanup,
                runs_dir=configurable.runs_dir,
                interval_seconds=float(
                    os.getenv("LITELLM_RUN_KEY_RECONCILE_INTERVAL_SECONDS", "300")
                ),
            )
        )
    try:
        yield
    finally:
        _shutting_down.set()
        try:
            await _drain_inflight_runs(
                configurable.shutdown_drain_timeout_seconds
            )
        finally:
            _sse_shutdown.set()
            if _native_research_service is not None:
                await _native_research_service.native_aclose()
                _native_research_service = None
            if _retention_sweep_task is not None:
                _retention_sweep_task.cancel()
                await asyncio.gather(_retention_sweep_task, return_exceptions=True)
                _retention_sweep_task = None
            if _run_key_reconciler_task is not None:
                _run_key_reconciler_task.cancel()
                await asyncio.gather(
                    _run_key_reconciler_task,
                    return_exceptions=True,
                )
                _run_key_reconciler_task = None
            for task in list(_run_eviction_tasks.values()):
                task.cancel()
            if _run_eviction_tasks:
                await asyncio.gather(
                    *_run_eviction_tasks.values(),
                    return_exceptions=True,
                )
            _run_eviction_tasks.clear()
            from open_deep_research.tasks.team_runtime import team_runtime
            await team_runtime.close()
            await close_document_pool()
            await close_embedding_clients()
            await shutdown_rbac()


app = FastAPI(title="Open Deep Research", version="0.1.0", lifespan=_lifespan)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next: Any) -> Response:
    """Bind and echo a gateway request ID for logs, runs, and traces."""
    supplied = str(request.headers.get("X-Request-ID") or "").strip()
    request_id = (
        supplied
        if 0 < len(supplied) <= 128
        and re.fullmatch(r"[A-Za-z0-9._:-]+", supplied)
        else str(uuid.uuid4())
    )
    bind_request_id(request_id)
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.middleware("http")
async def request_body_limit_middleware(request: Request, call_next: Any) -> Response:
    """Reject declared and streaming request bodies above the configured cap."""
    if request.method not in {"POST", "PUT", "PATCH"}:
        return await call_next(request)
    is_document_upload = request.method == "POST" and request.url.path.rstrip("/") == "/documents"
    limit = (
        get_document_settings().max_file_bytes + 2 * 1024 * 1024
        if is_document_upload
        else Configuration.from_runnable_config(None).max_request_body_bytes
    )
    content_length = request.headers.get("Content-Length")
    if is_document_upload and content_length is None:
        return JSONResponse({"detail": "content_length_required"}, status_code=411)
    if content_length is not None:
        try:
            if int(content_length) > limit:
                return JSONResponse(
                    {"detail": "request_body_too_large"},
                    status_code=413,
                )
        except ValueError:
            return JSONResponse({"detail": "invalid_content_length"}, status_code=400)
    if is_document_upload:
        # Content-Length is required before Starlette parses and spools multipart
        # data. stage_upload remains the per-file authoritative safety boundary.
        return await call_next(request)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            return JSONResponse(
                {"detail": "request_body_too_large"},
                status_code=413,
            )
    request._body = bytes(body)  # noqa: SLF001 - Starlette replays the bounded body
    return await call_next(request)
_allowed_origins = [
    item.strip()
    for item in os.environ.get(
        "FRONTEND_ALLOWED_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000",
    ).split(",")
    if item.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "Last-Event-ID"],
)

# Self-hosted identity & RBAC subsystem. Supabase is intentionally not mounted.
mount_rbac(app)
app.include_router(documents_router)
app.include_router(knowledge_router)
app.include_router(knowledge_search_router)
app.include_router(batch_router)
app.include_router(fact_wiki_router)
app.include_router(health_router)
app.include_router(workspace_router)


async def _rbac_run_owner_checker(_db, principal, run_id: str) -> bool:
    """Ownership bridge used by ``require_run_owner`` (prepared for cutover)."""
    if _native_research_service is not None:
        try:
            await _native_research_service.store.load(run_id, principal.user_id)
            return True
        except KeyError:
            pass
    record = _runs.get(run_id)
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


async def _rbac_task_owner_checker(_db, principal, key: tuple[str, str]) -> bool:
    """Ownership bridge used by ``require_task_owner`` (prepared for cutover)."""
    run_id, task_id = key
    if not await _rbac_run_owner_checker(_db, principal, run_id):
        return False
    configurable = Configuration.from_runnable_config(None)
    projection = RunEventStore(run_id, runs_dir=configurable.runs_dir).project()
    return task_id in projection.task_items


register_ownership_checker("run", _rbac_run_owner_checker)
register_ownership_checker("task", _rbac_task_owner_checker)

logger = logging.getLogger(__name__)
_runs: dict[str, RunRecord] = {}
_run_eviction_tasks: dict[str, asyncio.Task[None]] = {}
_retention_sweep_task: asyncio.Task[None] | None = None
_run_key_reconciler_task: asyncio.Task[None] | None = None
_api_rate_limiter = FixedWindowRateLimiter()
_sse_connection_limiter = ConnectionLimiter()
_shutting_down = asyncio.Event()
_sse_shutdown = asyncio.Event()
_TERMINAL_RUN_STATUSES = frozenset(
    {"success", "completed", "failed", "interrupted", "cancelled"}
)
_metrics_path = Configuration.from_runnable_config(None).prometheus_metrics_path


def _resolve_internal_sandbox_run(run_id: str) -> InternalRunContext | None:
    """Resolve the live, fenced API authority for trusted sandbox services."""
    record = _runs.get(run_id)
    if record is None or record.engine.run_fence_token is None:
        return None
    config = record.engine.config
    return InternalRunContext(
        config=config,
        configurable=Configuration.from_runnable_config(config),
        fence_token=int(record.engine.run_fence_token),
        started_at=float(record.engine.started_at),
    )


_native_research_service = None


async def _native_key_cleanup(run_id, manager):
    if _native_research_service is None:
        return False
    from open_deep_research.agentscope_runtime.native_security import cleanup_run_key
    return await cleanup_run_key(_native_research_service.store, run_id, manager)


async def _native_sandbox_ledger(run_id: str):
    """原生运行的 SQL 账本权威；仅网关计账的活跃运行返回账本。

    旧引擎运行与本缝未启用时返回 None，内部预算端点保持文件账本行为。
    """
    service = _native_research_service
    if service is None:
        return None
    return await service.pipeline_factory.gateway_ledger(run_id)


app.include_router(
    build_internal_sandbox_router(
        _resolve_internal_sandbox_run, native_ledger=_native_sandbox_ledger,
        native_root_key=lambda: Configuration.from_runnable_config(None).sandbox_root_signing_key,
    )
)


def _new_run_record(
    *,
    run_id: str,
    engine: QueryEngine,
    status: str,
    config: dict[str, Any] | None,
) -> RunRecord:
    """Build a run record with the configured bounded event buffer."""
    configurable = Configuration.from_runnable_config(config)
    return RunRecord(
        run_id=run_id,
        engine=engine,
        status=status,
        events=deque(maxlen=configurable.inflight_event_buffer_size),
    )


def _evict_run_record(
    run_id: str,
    *,
    expected_finished_at: float,
    reason: str,
) -> bool:
    """Evict the matching terminal record without touching a resumed replacement."""
    record = _runs.get(run_id)
    if (
        record is None
        or record.status not in _TERMINAL_RUN_STATUSES
        or record.finished_at != expected_finished_at
    ):
        return False
    _runs.pop(run_id, None)
    eviction_task = _run_eviction_tasks.pop(run_id, None)
    try:
        current_task = asyncio.current_task()
    except RuntimeError:
        current_task = None
    if eviction_task is not None and eviction_task is not current_task:
        eviction_task.cancel()
    logger.info(
        "run.evicted actor=system action=run.evicted run_id=%s reason=%s",
        run_id,
        reason,
        extra={
            "actor": "system",
            "action": "run.evicted",
            "run_id": run_id,
            "reason": reason,
        },
    )
    return True


def _enforce_run_memory_limit(maximum: int) -> None:
    """Evict least-recently-finished terminal runs until the soft cap is met."""
    while len(_runs) > maximum:
        candidates = [
            record
            for record in _runs.values()
            if record.status in _TERMINAL_RUN_STATUSES
            and record.finished_at is not None
        ]
        if not candidates:
            return
        oldest = min(candidates, key=lambda item: item.finished_at or 0.0)
        _evict_run_record(
            oldest.run_id,
            expected_finished_at=oldest.finished_at or 0.0,
            reason="capacity",
        )


def _remember_run(record: RunRecord, config: dict[str, Any] | None) -> None:
    """Register a live run and enforce the process-local record cap."""
    configurable = Configuration.from_runnable_config(config)
    if record.events.maxlen != configurable.inflight_event_buffer_size:
        record.events = deque(
            record.events,
            maxlen=configurable.inflight_event_buffer_size,
        )
    stale_task = _run_eviction_tasks.pop(record.run_id, None)
    if stale_task is not None:
        stale_task.cancel()
    _runs[record.run_id] = record
    _enforce_run_memory_limit(configurable.max_inflight_runs_in_memory)


async def _evict_run_after_delay(
    run_id: str,
    expected_finished_at: float,
    delay_seconds: float,
) -> None:
    """Evict a terminal run after its configured in-memory grace period."""
    try:
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        _evict_run_record(
            run_id,
            expected_finished_at=expected_finished_at,
            reason="retention",
        )
    finally:
        try:
            current_task = asyncio.current_task()
        except RuntimeError:
            current_task = None
        if _run_eviction_tasks.get(run_id) is current_task:
            _run_eviction_tasks.pop(run_id, None)


def _schedule_run_eviction(
    record: RunRecord,
    config: dict[str, Any] | None,
) -> None:
    """Mark a terminal record finished and schedule bounded retention."""
    if record.status not in _TERMINAL_RUN_STATUSES:
        return
    if record.finished_at is None:
        record.finished_at = time.time()
    configurable = Configuration.from_runnable_config(config)
    _enforce_run_memory_limit(configurable.max_inflight_runs_in_memory)
    if _runs.get(record.run_id) is not record:
        return
    previous = _run_eviction_tasks.pop(record.run_id, None)
    if previous is not None:
        previous.cancel()
    _run_eviction_tasks[record.run_id] = asyncio.create_task(
        _evict_run_after_delay(
            record.run_id,
            record.finished_at,
            configurable.inflight_run_memory_retention_seconds,
        )
    )

FRONTEND_EDITABLE_CONFIG_KEYS = (
    "allow_clarification", "enable_async_research", "enable_human_in_loop",
    "summarization_model", "summarization_model_max_tokens",
    "research_model", "research_model_max_tokens",
    "compression_model", "compression_model_max_tokens",
    "final_report_model", "final_report_model_max_tokens",
    "search_api", "web_pipeline_mode", "web_pipeline_shadow_sample_rate",
    "web_min_source_authority", "search_candidate_limit",
    "max_fetches_per_researcher", "max_concurrent_research_units",
    "max_researcher_iterations", "max_react_tool_calls",
    "hitl_require_plan_approval", "hitl_require_outline_approval",
    "hitl_max_plan_revisions", "hitl_feedback_mode", "report_type", "output_format",
    "quality_evaluation_enabled", "quality_evaluation_model",
    "quality_evaluation_model_max_tokens", "quality_evaluation_rigor",
    "quality_evaluation_min_sources", "quality_evaluation_max_input_chars",
    "quality_risk_mode", "quality_evaluation_fail_open",
    "quality_caveat_admission_enabled", "quality_gap_recovery_max_attempts",
    "report_review_enabled", "report_review_model",
    "report_review_model_max_tokens", "report_review_temperature",
    "report_review_max_input_chars", "report_review_max_revisions",
    "report_review_fail_open",
    "enable_memory", "memory_top_k", "memory_min_confidence", "memory_auto_write",
    "memory_write_after_report", "memory_fail_open", "memory_advanced_enabled",
    "memory_decay_enabled", "memory_reflection_enabled", "memory_profile_enabled",
    "memory_legacy_recall_enabled", "memory_run_end_maintenance_enabled",
    "memory_mutation_lock_timeout_seconds",
    "memory_soft_forgetting_enabled", "memory_verified_insights_enabled",
    "memory_search_threshold", "memory_search_rerank", "memory_importance_weight",
    "memory_relevance_weight", "memory_recency_weight",
    "memory_reflection_observation_threshold", "memory_reflection_importance_threshold",
    "memory_reflection_max_age_hours", "memory_maintenance_max_input_chars",
    "memory_profile_max_chars", "memory_half_life_days",
)


def _runs_dir_size_bytes(root: Path) -> int:
    """Return durable artifact bytes without following symlinks outside runs_dir."""
    if not root.exists():
        return 0
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            total += path.stat().st_size
        except OSError:
            continue
    return total


async def _refresh_operational_metrics() -> None:
    """Refresh scrape-time gauges while keeping metrics export fail-open."""
    configurable = Configuration.from_runnable_config(None)
    metrics = get_prometheus_metrics(configurable)
    if metrics is None:
        return
    try:
        used_bytes = await asyncio.to_thread(
            _runs_dir_size_bytes,
            Path(configurable.runs_dir),
        )
        metrics.set_runs_dir_usage(used_bytes, configurable.runs_dir_max_bytes)
    except Exception as exc:  # noqa: BLE001 - metrics must never block the API
        with contextlib.suppress(Exception):
            metrics.observe_export_error("prometheus", "runs_dir_usage")
        logger.debug("Unable to refresh runs_dir metrics: %s", exc)
    try:
        metrics.set_model_circuit_states(
            await get_model_circuit_registry().snapshots()
        )
    except Exception as exc:  # noqa: BLE001 - metrics must never block the API
        with contextlib.suppress(Exception):
            metrics.observe_export_error("prometheus", "model_circuit_state")
        logger.debug("Unable to refresh model circuit metrics: %s", exc)
    if get_document_settings().configured:
        try:
            metrics.set_document_operational(await document_operational_snapshot())
        except Exception as exc:  # noqa: BLE001 - metrics must never block the API
            with contextlib.suppress(Exception):
                metrics.observe_export_error("prometheus", "document_operational")
            logger.debug("Unable to refresh document metrics: %s", exc)


@app.get(_metrics_path, include_in_schema=False)
async def prometheus_metrics() -> Response:
    """Expose process-wide aggregate metrics for Prometheus scraping."""
    await _refresh_operational_metrics()
    body = generate_latest()
    configurable = Configuration.from_runnable_config(None)
    if (_native_research_service is not None
            and configurable.observability_enabled and configurable.prometheus_enabled):
        from open_deep_research.agentscope_runtime.telemetry import prometheus_snapshot
        body += await prometheus_snapshot(_native_research_service.store)
    return Response(content=body, media_type=CONTENT_TYPE_LATEST)


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Report process liveness without consulting dependencies."""
    return {"status": "ok"}


def _probe_runs_directory(configurable: Configuration) -> None:
    """Raise unless the configured run directory supports create and delete."""
    root = Path(configurable.runs_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    probe = root / f".readiness-{uuid.uuid4().hex}.tmp"
    try:
        probe.write_bytes(b"ok")
    finally:
        with contextlib.suppress(OSError):
            probe.unlink()


def _search_readiness(configurable: Configuration) -> dict[str, str]:
    """Describe search credential availability without making network calls."""
    provider = getattr(configurable.search_api, "value", str(configurable.search_api))
    if provider == "none":
        return {"status": "disabled", "provider": provider}
    key_name = {
        "tavily": "TAVILY_API_KEY",
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
    }.get(provider)
    if key_name and os.environ.get(key_name):
        return {"status": "ok", "provider": provider}
    return {"status": "degraded", "provider": provider, "reason": "api_key_missing"}


async def _readiness_report() -> tuple[dict[str, Any], bool]:
    """Probe critical local dependencies and non-critical search credentials."""
    configurable = Configuration.from_runnable_config(None)
    components: dict[str, dict[str, Any]] = {}
    critical_ok = True

    if _shutting_down.is_set():
        components["server"] = {"status": "failed", "reason": "shutting_down"}
        critical_ok = False
    else:
        components["server"] = {"status": "ok"}

    try:
        await asyncio.to_thread(_probe_runs_directory, configurable)
        components["runs_dir"] = {"status": "ok"}
    except Exception as exc:  # noqa: BLE001 - readiness reports the failure
        components["runs_dir"] = {
            "status": "failed",
            "error_type": type(exc).__name__,
        }
        critical_ok = False

    if configurable.observability_enabled and configurable.sqlite_observability_enabled:
        try:
            store = await asyncio.to_thread(
                SQLiteTraceStore,
                configurable.trace_store_path,
            )
            await asyncio.to_thread(store.ping)
            components["trace_store"] = {"status": "ok"}
        except Exception as exc:  # noqa: BLE001 - readiness reports the failure
            components["trace_store"] = {
                "status": "failed",
                "error_type": type(exc).__name__,
            }
            critical_ok = False
    else:
        components["trace_store"] = {"status": "disabled"}

    iam_settings = get_iam_settings()
    if iam_settings.database_url:
        try:
            await check_database_connection(iam_settings)
            components["iam_database"] = {"status": "ok"}
        except Exception as exc:  # noqa: BLE001 - readiness reports the failure
            components["iam_database"] = {
                "status": "failed",
                "error_type": type(exc).__name__,
            }
            critical_ok = False
    else:
        components["iam_database"] = {"status": "disabled"}

    if configurable.sandbox_enabled:
        from open_deep_research.sandbox.doctor import diagnose

        sandbox_report = await asyncio.to_thread(diagnose)
        components["sandbox"] = sandbox_report
        if not sandbox_report.get("ready"):
            critical_ok = False
    else:
        components["sandbox"] = {"status": "disabled"}

    if configurable.model_backend == "litellm":
        try:
            settings = RunKeySettings.from_env()
            base_url = settings.base_url.rstrip("/")
            if base_url.endswith("/v1"):
                base_url = base_url[:-3]
            async with httpx.AsyncClient(
                base_url=base_url,
                headers={"Authorization": f"Bearer {settings.master_key}"},
                timeout=5,
            ) as client:
                response = await client.get("/health/readiness")
                response.raise_for_status()
            components["litellm"] = {"status": "ok"}
        except Exception as exc:  # noqa: BLE001 - readiness is content-free
            components["litellm"] = {
                "status": "failed",
                "error_type": type(exc).__name__,
            }
            critical_ok = False
    else:
        components["litellm"] = {"status": "disabled"}

    components["search"] = _search_readiness(configurable)
    overall_status = "ok" if critical_ok else "failed"
    if critical_ok and components["search"]["status"] == "degraded":
        overall_status = "degraded"
    return {"status": overall_status, "components": components}, critical_ok


@app.get("/readyz", include_in_schema=False)
async def readyz() -> JSONResponse:
    """Report whether this process can safely receive new traffic."""
    report, ready = await _readiness_report()
    return JSONResponse(report, status_code=200 if ready else 503)


async def _reauthorize_stream(principal):
    async with session_scope() as db:
        return await reauthorize_session(db, principal) is not None


def _stream_options():
    return StreamOptions(
        configuration=Configuration.from_runnable_config(None),
        shutdown=_sse_shutdown,
        authorize=_reauthorize_stream,
        reauth_interval=get_iam_settings().sse_reauth_interval,
        publisher_settings=get_publisher_settings(),
    )


async def _public_event_iterator(store, *, after=0, principal=None):
    async for frame in streams._public_event_iterator(
        store, after=after, principal=principal, options=_stream_options()
    ):
        yield frame


async def _publication_event_iterator(store, *, after=0, principal=None):
    async for frame in streams._publication_event_iterator(
        store, after=after, principal=principal, options=_stream_options()
    ):
        yield frame


async def _task_activity_iterator(store, *, after=0, principal=None):
    async for frame in streams._task_activity_iterator(
        store, after=after, principal=principal, options=_stream_options()
    ):
        yield frame


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


def _observability_store() -> SQLiteTraceStore:
    configurable = Configuration.from_runnable_config(None)
    return SQLiteTraceStore(configurable.trace_store_path)


def _user_identity(user: Principal) -> str:
    """Return the normalized authenticated identity used by run ownership checks."""
    return user.user_id


def _principal_kind(user: Principal) -> str:
    """Return a bounded metric label for authenticated principal provenance."""
    return "development" if user.user_id == "local-dev-user" else "authenticated"


def _api_governance_metrics(configurable: Configuration) -> Any:
    return get_prometheus_metrics(configurable)


def _observe_rate_limited(
    configurable: Configuration,
    dimension: str,
    user: Principal,
) -> None:
    metrics = _api_governance_metrics(configurable)
    if metrics is not None:
        with contextlib.suppress(Exception):
            metrics.observe_api_rate_limited(dimension, _principal_kind(user))
    logger.info(
        "API request rate limited",
        extra={
            "actor": _user_identity(user),
            "action": "api.rate_limited",
            "dimension": dimension,
            "principal_kind": _principal_kind(user),
        },
    )


def _observe_limiter_error(
    configurable: Configuration,
    dimension: str,
    exc: BaseException,
) -> None:
    metrics = _api_governance_metrics(configurable)
    if metrics is not None:
        with contextlib.suppress(Exception):
            metrics.observe_rate_limiter_error(dimension)
    logger.warning(
        "API rate limiter failed open",
        extra={"action": "api.rate_limiter_error", "dimension": dimension},
        exc_info=exc,
    )


def _active_runs_for_user(user_id: str) -> int:
    """Count process-local non-terminal runs owned by one principal."""
    active = 0
    for record in _runs.values():
        if record.status in _TERMINAL_RUN_STATUSES:
            continue
        metadata = getattr(record.engine, "config", {}).get("metadata", {})
        owner = metadata.get("owner") or metadata.get("user_id")
        if str(owner or "") == user_id:
            active += 1
    return active


def _enforce_run_create_limits(user: Principal, configurable: Configuration) -> None:
    """Apply per-principal creation and active-run limits, failing open on bugs."""
    identity = _user_identity(user)
    try:
        allowed, retry_after = _api_rate_limiter.allow(
            f"run-create:{identity}",
            configurable.api_run_create_per_minute,
        )
        if not allowed:
            _observe_rate_limited(configurable, "run_create_rate", user)
            raise HTTPException(
                status_code=429,
                detail="run_create_rate_limited",
                headers={"Retry-After": str(retry_after)},
            )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - business limiter is fail-open
        _observe_limiter_error(configurable, "run_create_rate", exc)

    try:
        maximum = configurable.max_concurrent_runs_per_user
        if maximum > 0 and _active_runs_for_user(identity) >= maximum:
            _observe_rate_limited(configurable, "concurrent_runs", user)
            raise HTTPException(
                status_code=429,
                detail="concurrent_run_limit_reached",
                headers={"Retry-After": "5"},
            )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - business limiter is fail-open
        _observe_limiter_error(configurable, "concurrent_runs", exc)


async def _reserve_sse_connection(
    user: Principal,
    configurable: Configuration,
) -> int:
    """Reserve one global SSE slot and return the release token (configured cap)."""
    limit = configurable.max_concurrent_sse_connections
    try:
        allowed = await _sse_connection_limiter.acquire(limit)
    except Exception as exc:  # noqa: BLE001 - business limiter is fail-open
        _observe_limiter_error(configurable, "sse_connections", exc)
        return 0
    if not allowed:
        _observe_rate_limited(configurable, "sse_connections", user)
        raise HTTPException(
            status_code=429,
            detail="sse_connection_limit_reached",
            headers={"Retry-After": "5"},
        )
    return limit


async def _limited_sse(source: Any, release_token: int):
    """Release a global SSE slot whenever iteration ends or disconnects."""
    try:
        async for item in source:
            yield item
    finally:
        with contextlib.suppress(Exception):
            await _sse_connection_limiter.release(release_token)


def _request_query_preview(request: RunRequest) -> str:
    for message in request.messages:
        if str(message.get("role", "")).lower() in {"user", "human"}:
            content = message.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    str(item.get("text", "")) if isinstance(item, dict) else str(item)
                    for item in content
                )
            return " ".join(str(content).split())[:280]
    return ""


def _run_title(request: RunRequest, run_id: str) -> str:
    explicit = " ".join((request.title or "").split())
    preview = _request_query_preview(request)
    return (explicit or preview[:80] or run_id)[:160]


def _runs_root(config: dict[str, Any] | None = None) -> Path:
    return Path(Configuration.from_runnable_config(config).runs_dir).resolve()


def _load_manifests(config: dict[str, Any] | None = None) -> list[Any]:
    return _load_manifests_from_root(_runs_root(config))


def _load_manifests_from_root(root: Path) -> list[Any]:
    """Load manifests from an already resolved root without re-reading env config."""
    if not root.exists():
        return []
    manifests = []
    for path in root.glob("*/context/manifest.json"):
        try:
            manifests.append(
                RunContextStore(path.parent.parent.name, runs_dir=str(root)).load_manifest()
            )
        except (ValueError, JournalCorruptedError, OSError):
            continue
    return manifests


def _fenced_recovery_config(
    manifest: Any,
    configurable: Configuration,
    lease_manager: LeaderLeaseManager,
    fence_token: int,
) -> dict[str, Any]:
    """Build the minimal config needed for a fenced recovery event write."""
    stored = manifest.config if isinstance(manifest.config, dict) else {}
    return {
        "configurable": {
            **dict(stored.get("configurable") or {}),
            "runs_dir": configurable.runs_dir,
        },
        "metadata": {
            **dict(stored.get("metadata") or {}),
            "run_id": manifest.run_id,
            "run_fence_token": fence_token,
            "run_lease_owner_id": lease_manager.owner_id,
        },
    }


async def _renew_sweep_lease(
    lease_manager: LeaderLeaseManager,
    fence_token: int,
    interval_seconds: float,
) -> None:
    """Keep the global recovery-sweep lease live for the whole scan."""
    while True:
        await asyncio.sleep(interval_seconds)
        await lease_manager.renew(expected_fence_token=fence_token)


async def _run_recovery_sweep(configurable: Configuration) -> int:
    """Interrupt orphaned non-terminal manifests while preserving live owners."""
    started_at = time.perf_counter()
    sweep_lease = LeaderLeaseManager(
        runs_dir=configurable.runs_dir,
        run_id="system-recovery-sweep",
        lease_seconds=configurable.leader_lease_seconds,
        lock_timeout=configurable.mailbox_lock_timeout_seconds,
    )
    try:
        global_lease = await sweep_lease.acquire()
    except LeaseConflictError:
        logger.info(
            "run recovery sweep skipped actor=system action=run.recovery_sweep "
            "reason=live_sweep_owner"
        )
        return 0

    renew_task = asyncio.create_task(
        _renew_sweep_lease(
            sweep_lease,
            global_lease.fence_token,
            max(0.1, configurable.leader_heartbeat_seconds),
        )
    )
    interrupted = 0
    try:
        for manifest in await asyncio.to_thread(
            _load_manifests,
            {"configurable": {"runs_dir": configurable.runs_dir}},
        ):
            if manifest.status in _TERMINAL_RUN_STATUSES:
                continue
            run_lease = LeaderLeaseManager(
                runs_dir=configurable.runs_dir,
                run_id=manifest.run_id,
                lease_seconds=configurable.leader_lease_seconds,
                lock_timeout=configurable.mailbox_lock_timeout_seconds,
            )
            try:
                lease = await run_lease.acquire()
            except LeaseConflictError:
                continue
            try:
                store = RunContextStore(
                    manifest.run_id,
                    runs_dir=configurable.runs_dir,
                )
                await asyncio.to_thread(
                    store.bind_fence_token,
                    lease.fence_token,
                    run_lease.owner_id,
                )
                await asyncio.to_thread(
                    store._update_manifest,  # noqa: SLF001
                    status="interrupted",
                )
                event_config = _fenced_recovery_config(
                    manifest,
                    configurable,
                    run_lease,
                    lease.fence_token,
                )
                interrupted += 1
                try:
                    await event_publisher_from_config(event_config).publish(
                        "run.interrupted",
                        payload={
                            "status": "interrupted",
                            "error_code": "startup_recovery_sweep",
                            "message": "The previous process stopped before this run completed.",
                            "termination_reason": "orphaned_run",
                            "result_status": "interrupted",
                        },
                        dedupe_key=f"run:interrupted:startup:{manifest.run_id}",
                    )
                except Exception as exc:  # noqa: BLE001 - event export is fail-open
                    logger.warning(
                        "run recovery event write failed actor=system "
                        "action=run.recovery_sweep run_id=%s error_type=%s",
                        manifest.run_id,
                        type(exc).__name__,
                    )
                try:
                    await asyncio.to_thread(
                        get_trace_recorder(event_config).finish_run,
                        manifest.run_id,
                        "interrupted",
                    )
                except Exception as exc:  # noqa: BLE001 - trace export is fail-open
                    logger.warning(
                        "run recovery trace write failed actor=system "
                        "action=run.recovery_sweep run_id=%s error_type=%s",
                        manifest.run_id,
                        type(exc).__name__,
                    )
            except Exception as exc:  # noqa: BLE001 - one bad run cannot block startup
                logger.warning(
                    "run recovery failed actor=system action=run.recovery_sweep "
                    "run_id=%s error_type=%s",
                    manifest.run_id,
                    type(exc).__name__,
                )
            finally:
                with contextlib.suppress(Exception):
                    await run_lease.release(
                        expected_fence_token=lease.fence_token
                    )
    finally:
        renew_task.cancel()
        await asyncio.gather(renew_task, return_exceptions=True)
        with contextlib.suppress(Exception):
            await sweep_lease.release(
                expected_fence_token=global_lease.fence_token
            )

    logger.info(
        "run recovery sweep completed actor=system action=run.recovery_sweep "
        "interrupted=%s duration_seconds=%.3f",
        interrupted,
        time.perf_counter() - started_at,
    )
    return interrupted


def _run_finished_at(manifest: Any) -> float:
    """Return the durable terminal timestamp, including legacy manifests."""
    return float(manifest.ended_at or manifest.updated_at or manifest.created_at)


def _run_directory(configurable: Configuration, run_id: str) -> Path:
    """Resolve a run directory while preventing cleanup path traversal."""
    root = Path(configurable.runs_dir).resolve()
    target = (root / run_id).resolve()
    if target == root or root not in target.parents:
        raise ValueError("run_id escapes runs_dir")
    return target


def _lifecycle_metrics(configurable: Configuration) -> Any:
    """Return enabled Prometheus collectors for fail-open lifecycle reporting."""
    return get_prometheus_metrics(configurable)


def _record_lifecycle_error(
    configurable: Configuration,
    operation: str,
    exc: BaseException,
) -> None:
    """Report a cleanup failure without allowing metrics to mask the cause."""
    metrics = _lifecycle_metrics(configurable)
    if metrics is not None:
        with contextlib.suppress(Exception):
            metrics.observe_export_error("retention", operation)
    logger.warning(
        "run lifecycle cleanup failed actor=system action=%s error_type=%s",
        operation,
        type(exc).__name__,
    )


async def _purge_run_artifacts(
    run_id: str,
    configurable: Configuration,
    *,
    reason: str,
    actor: str,
    require_terminal: bool = True,
) -> dict[str, Any]:
    """Delete disk, trace, and memory state through one idempotent path."""
    target = _run_directory(configurable, run_id)
    record = _runs.get(run_id)
    manifest = None
    if target.exists():
        try:
            manifest = await asyncio.to_thread(
                RunContextStore(run_id, runs_dir=configurable.runs_dir).load_manifest
            )
        except (JournalCorruptedError, OSError, ValueError):
            manifest = None
    status = record.status if record is not None else getattr(manifest, "status", None)
    if require_terminal and status not in _TERMINAL_RUN_STATUSES:
        raise RuntimeError("run_not_terminal")

    logger.info(
        "run purge started actor=%s action=run.purge run_id=%s reason=%s",
        actor,
        run_id,
        reason,
        extra={"actor": actor, "action": "run.purge", "run_id": run_id, "reason": reason},
    )
    if get_document_settings().configured:
        with contextlib.suppress(Exception):
            await release_run_sources(run_id)
    trace_rows = await asyncio.to_thread(
        SQLiteTraceStore(configurable.trace_store_path).delete_run,
        run_id,
    )
    directory_existed = target.exists()
    if directory_existed:
        await asyncio.to_thread(shutil.rmtree, target)
    eviction_task = _run_eviction_tasks.pop(run_id, None)
    if eviction_task is not None:
        eviction_task.cancel()
    _runs.pop(run_id, None)
    metrics = _lifecycle_metrics(configurable)
    if metrics is not None:
        with contextlib.suppress(Exception):
            metrics.observe_run_purged(reason)
    logger.info(
        "run purge completed actor=%s action=run.purge run_id=%s reason=%s "
        "trace_rows=%s directory_existed=%s",
        actor,
        run_id,
        reason,
        trace_rows,
        directory_existed,
        extra={"actor": actor, "action": "run.purge", "run_id": run_id, "reason": reason},
    )
    return {
        "run_id": run_id,
        "status": "deleted",
        "reason": reason,
        "directory_deleted": directory_existed,
        "trace_rows_deleted": trace_rows,
    }


async def _run_retention_sweep(configurable: Configuration) -> dict[str, Any]:
    """Apply age retention, trace retention, and quota fallback once."""
    started_at = time.perf_counter()
    metrics = _lifecycle_metrics(configurable)
    deleted_by_age = 0
    deleted_by_quota = 0
    trace_runs_deleted = 0
    sweep_lease = LeaderLeaseManager(
        runs_dir=configurable.runs_dir,
        run_id="system-retention-sweep",
        lease_seconds=configurable.leader_lease_seconds,
        lock_timeout=configurable.mailbox_lock_timeout_seconds,
    )
    try:
        lease = await sweep_lease.acquire()
    except LeaseConflictError:
        return {
            "status": "skipped",
            "reason": "live_sweep_owner",
            "deleted_by_age": 0,
            "deleted_by_quota": 0,
        }

    try:
        manifests = await asyncio.to_thread(
            _load_manifests_from_root,
            Path(configurable.runs_dir).resolve(),
        )
        terminal = [
            item for item in manifests if item.status in _TERMINAL_RUN_STATUSES
        ]
        if configurable.run_retention_days > 0:
            cutoff = time.time() - configurable.run_retention_days * 86400
            for manifest in sorted(terminal, key=_run_finished_at):
                if _run_finished_at(manifest) >= cutoff:
                    continue
                try:
                    await _purge_run_artifacts(
                        manifest.run_id,
                        configurable,
                        reason="retention",
                        actor="system",
                    )
                    deleted_by_age += 1
                except Exception as exc:  # noqa: BLE001 - sweep is fail-open
                    _record_lifecycle_error(configurable, "retention_purge", exc)

        trace_days = (
            configurable.run_retention_days
            if configurable.trace_retention_days is None
            else configurable.trace_retention_days
        )
        trace_store = SQLiteTraceStore(configurable.trace_store_path)
        if trace_days > 0:
            trace_cutoff = time.time() - trace_days * 86400
            try:
                trace_runs_deleted = await asyncio.to_thread(
                    trace_store.delete_runs_ended_before,
                    trace_cutoff,
                )
            except Exception as exc:  # noqa: BLE001 - sweep is fail-open
                _record_lifecycle_error(configurable, "trace_retention", exc)

        quota = configurable.runs_dir_max_bytes
        if quota > 0:
            target_bytes = int(quota * 0.9)
            used_bytes = await asyncio.to_thread(
                _runs_dir_size_bytes,
                Path(configurable.runs_dir),
            )
            quota_triggered = used_bytes > quota
            if quota_triggered:
                remaining = [
                    item
                    for item in terminal
                    if _run_directory(configurable, item.run_id).exists()
                ]
                for manifest in sorted(remaining, key=_run_finished_at):
                    if used_bytes <= target_bytes:
                        break
                    try:
                        await _purge_run_artifacts(
                            manifest.run_id,
                            configurable,
                            reason="quota",
                            actor="system",
                        )
                        deleted_by_quota += 1
                        await asyncio.to_thread(trace_store.checkpoint)
                        used_bytes = await asyncio.to_thread(
                            _runs_dir_size_bytes,
                            Path(configurable.runs_dir),
                        )
                    except Exception as exc:  # noqa: BLE001 - sweep is fail-open
                        _record_lifecycle_error(configurable, "quota_purge", exc)
            quota_exceeded = quota_triggered and used_bytes > target_bytes
            if metrics is not None:
                with contextlib.suppress(Exception):
                    metrics.set_runs_dir_usage(used_bytes, quota)
                    metrics.set_retention_quota_exceeded(quota_exceeded)
            if quota_exceeded:
                logger.error(
                    "run retention quota remains exceeded actor=system "
                    "action=run.retention_sweep used_bytes=%s quota_bytes=%s",
                    used_bytes,
                    quota,
                )
        elif metrics is not None:
            with contextlib.suppress(Exception):
                metrics.set_retention_quota_exceeded(False)

        with contextlib.suppress(Exception):
            await asyncio.to_thread(trace_store.checkpoint)
    finally:
        with contextlib.suppress(Exception):
            await sweep_lease.release(expected_fence_token=lease.fence_token)
        duration = time.perf_counter() - started_at
        if metrics is not None:
            with contextlib.suppress(Exception):
                metrics.observe_retention_sweep(duration)

    logger.info(
        "run retention sweep completed actor=system action=run.retention_sweep "
        "deleted_by_age=%s deleted_by_quota=%s trace_runs_deleted=%s "
        "duration_seconds=%.3f",
        deleted_by_age,
        deleted_by_quota,
        trace_runs_deleted,
        time.perf_counter() - started_at,
        extra={"actor": "system", "action": "run.retention_sweep"},
    )
    return {
        "status": "completed",
        "deleted_by_age": deleted_by_age,
        "deleted_by_quota": deleted_by_quota,
        "trace_runs_deleted": trace_runs_deleted,
    }


async def _retention_sweep_loop(configurable: Configuration) -> None:
    """Run lifecycle cleanup periodically until service shutdown."""
    interval = configurable.retention_sweep_interval_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            await _run_retention_sweep(configurable)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - maintenance remains fail-open
            _record_lifecycle_error(configurable, "retention_sweep", exc)


async def _interrupt_inflight_record(record: RunRecord) -> None:
    """Persist and cancel one process-owned run during graceful shutdown."""
    config = getattr(record.engine, "config", None) or {}
    event_config = {
        "configurable": dict(config.get("configurable") or {}),
        "metadata": {
            **dict(config.get("metadata") or {}),
            "run_id": record.run_id,
        },
    }
    store = getattr(record.engine, "context_store", None)
    if store is not None and store.manifest_path.exists():
        try:
            await asyncio.to_thread(
                store._update_manifest,  # noqa: SLF001
                status="interrupted",
            )
        except Exception as exc:  # noqa: BLE001 - shutdown remains best-effort
            logger.warning(
                "run interrupt manifest write failed actor=system "
                "action=run.interrupted run_id=%s error_type=%s",
                record.run_id,
                type(exc).__name__,
            )
    try:
        await event_publisher_from_config(event_config).publish(
            "run.interrupted",
            payload={
                "status": "interrupted",
                "error_code": "server_shutdown",
                "message": "The server stopped before this run completed.",
                "termination_reason": "server_shutdown",
                "result_status": "interrupted",
            },
            dedupe_key=f"run:interrupted:shutdown:{record.run_id}",
        )
    except Exception:
        pass
    try:
        await asyncio.to_thread(
            get_trace_recorder(event_config).finish_run,
            record.run_id,
            "interrupted",
        )
    except Exception as exc:  # noqa: BLE001 - observability must not block drain
        logger.warning(
            "run interrupt trace write failed actor=system "
            "action=run.interrupted run_id=%s error_type=%s",
            record.run_id,
            type(exc).__name__,
        )
    record.status = "interrupted"
    record.finished_at = time.time()
    logger.info(
        "run interrupted actor=system action=run.interrupted run_id=%s "
        "reason=server_shutdown",
        record.run_id,
    )
    if record.task is not None and not record.task.done():
        record.task.cancel()
        await asyncio.gather(record.task, return_exceptions=True)


async def _drain_inflight_runs(timeout_seconds: float) -> None:
    """Interrupt all live in-memory runs within one shutdown budget."""
    records = [
        record
        for record in list(_runs.values())
        if record.status not in _TERMINAL_RUN_STATUSES
    ]

    async def drain() -> None:
        for record in records:
            await _interrupt_inflight_record(record)

    try:
        async with asyncio.timeout(timeout_seconds):
            await drain()
    except TimeoutError:
        logger.error(
            "run shutdown drain timed out actor=system action=run.interrupted "
            "timeout_seconds=%s",
            timeout_seconds,
        )
        for record in records:
            if record.task is not None and not record.task.done():
                record.task.cancel()
        await asyncio.gather(
            *(record.task for record in records if record.task is not None),
            return_exceptions=True,
        )


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


def _encode_cursor(created_at: float, run_id: str) -> str:
    raw = json.dumps([created_at, run_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[float, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode())
        return float(value[0]), str(value[1])
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid_cursor") from exc


def _publication_response(job: PublicationJob) -> dict[str, Any]:
    """Return one publication with API URLs and no local path."""
    payload = job.public_dict()
    payload["status_url"] = (
        f"/runs/{job.run_id}/publications/{job.publication_id}"
    )
    payload["events_url"] = f"/runs/{job.run_id}/publications/events"
    payload["download_url"] = (
        f"/runs/{job.run_id}/publications/{job.publication_id}/download"
        if job.status == "completed" and job.artifact is not None
        else None
    )
    if isinstance(payload.get("artifact"), dict):
        payload["artifact"]["download_url"] = payload["download_url"]
    return payload


async def _run_publications(run_id: str, runs_dir: str) -> list[dict[str, Any]]:
    """Load a bounded publication list for a run snapshot."""
    try:
        store = PublicationJobStore(run_id, runs_dir=runs_dir)
        jobs = (await asyncio.to_thread(store.list))[:100]
    except (OSError, ValueError, portalocker.exceptions.LockException):
        return []
    return [_publication_response(job) for job in jobs]








def _require_record_owner(record: RunRecord, user: Principal) -> None:
    """Hide in-memory runs from users other than their owner."""
    metadata = getattr(record.engine, "config", {}).get("metadata", {})
    owner = metadata.get("owner") or metadata.get("user_id")
    if not owner or str(owner) != _user_identity(user):
        raise HTTPException(status_code=404, detail="Run not found")


def _require_run_owner(run_id: str, user: Principal) -> tuple[RunRecord | None, Configuration]:
    """Authorize an active or persisted run and return its effective config."""
    record = _runs.get(run_id)
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


def _task_activity_preview_allowed(user: Principal) -> bool:
    """Authorize bounded diagnostic previews without trusting the browser."""
    if local_dev_bypass_enabled():
        return True
    enabled = os.environ.get("TASK_ACTIVITY_PREVIEW_ENABLED", "false").lower() in {
        "1", "true", "yes", "on",
    }
    return enabled and user.has(RESEARCH_DIAGNOSTICS_PREVIEW.code)


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


def _require_task_in_run(
    run_id: str,
    task_id: str,
    configurable: Configuration,
) -> Any:
    """Return the public task projection or hide unknown/cross-run task IDs."""
    projection = RunEventStore(run_id, runs_dir=configurable.runs_dir).project()
    task = projection.task_items.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


def _span_tree_rows(spans: list[dict[str, Any]]) -> str:
    children: dict[str | None, list[dict[str, Any]]] = {}
    for span in spans:
        children.setdefault(span.get("parent_span_id"), []).append(span)

    rows: list[str] = []

    def visit(span: dict[str, Any], depth: int) -> None:
        status = html.escape(str(span.get("status") or ""))
        name = html.escape(str(span.get("name") or ""))
        kind = html.escape(str(span.get("kind") or ""))
        duration = span.get("duration_ms") or 0
        tokens = span.get("total_tokens") or 0
        retry_count = span.get("retry_count") or 0
        error_type = html.escape(str(span.get("error_type") or ""))
        error = html.escape(str(span.get("error") or ""))
        indent = "&nbsp;" * depth * 4
        cls = "error" if status == "error" else "ok"
        rows.append(
            f"<tr class='{cls}'><td>{indent}{name}</td><td>{kind}</td>"
            f"<td>{status}</td><td>{duration}</td><td>{tokens}</td>"
            f"<td>{retry_count}</td><td>{error_type}</td><td>{error}</td></tr>"
        )
        for child in children.get(span.get("span_id"), []):
            visit(child, depth + 1)

    roots = children.get(None, [])
    for root in roots:
        visit(root, 0)
    for span in spans:
        if span.get("parent_span_id") and span.get("parent_span_id") not in {s.get("span_id") for s in spans}:
            visit(span, 0)
    return "".join(rows)


async def _release_gateway_run(record: RunRecord, config: dict[str, Any]) -> None:
    """Erase ephemeral Gateway credentials after all run work has terminated."""
    configurable = Configuration.from_runnable_config(config)
    fence_token = getattr(record.engine, "run_fence_token", None)
    if not configurable.sandbox_enabled or fence_token is None:
        return
    try:
        from open_deep_research.sandbox.gateway_client import (
            SandboxGatewayControlClient,
        )

        await SandboxGatewayControlClient(configurable).unregister_run(
            run_id=record.run_id,
            fence_token=int(fence_token),
        )
    except Exception as exc:  # noqa: BLE001 - task tokens still expire independently
        logger.warning(
            "sandbox gateway credential cleanup failed run_id=%s error=%s",
            record.run_id,
            str(exc)[:500],
        )


async def _run_background(record: RunRecord, request: RunRequest, config: dict[str, Any]) -> None:
    record.status = "running"
    control_task = asyncio.create_task(_run_control_listener(record, config))
    try:
        async for event in record.engine.stream_message(request.messages, config):
            record.events.append(event)
            status = event.get("data", {}).get("status")
            if status in {
                "running",
                "awaiting_clarification",
                "awaiting_plan_approval",
                "awaiting_outline_approval",
                "awaiting_fetch_budget_approval",
                "completed",
                "failed",
                "cancelled",
            }:
                record.status = status
        record.result = record.engine.final_state
        record.status = record.engine.status
    except Exception as exc:  # noqa: BLE001 - surface in run state
        event = {"event": "run.failed", "data": {"run_id": record.run_id, "error": str(exc)}}
        record.events.append(event)
        record.result = {"result": {"status": "error", "error": str(exc)}}
        record.status = "failed"
        try:
            await event_publisher_from_config(config).publish(
                "run.failed",
                payload={"status": "failed", "error_code": "run_execution_failed", "message": "Research failed."},
                dedupe_key="run:terminal",
            )
        except Exception:
            pass
    finally:
        control_task.cancel()
        await asyncio.gather(control_task, return_exceptions=True)
        await _release_gateway_run(record, config)
        _schedule_run_eviction(record, config)


async def _run_resumed_background(record: RunRecord) -> None:
    """Continue a persisted Query run in the background."""
    record.status = "running"
    config = getattr(record.engine, "config", None)
    control_task = asyncio.create_task(_run_control_listener(record, config or {}))
    try:
        async for event in record.engine.stream_resume():
            record.events.append(event)
            status = event.get("data", {}).get("status")
            if status in {"running", "completed", "failed", "cancelled"}:
                record.status = status
        record.result = record.engine.final_state
        record.status = "cancelled" if record.engine.status == "cancelled" else record.engine.status
    except Exception as exc:  # noqa: BLE001 - surface through run state
        record.events.append({"event": "run.failed", "data": {"run_id": record.run_id, "error": str(exc)}})
        record.result = {"result": {"status": "error", "error": str(exc)}}
        record.status = "failed"
        try:
            await event_publisher_from_config(config).publish(
                "run.failed",
                payload={"status": "failed", "error_code": "run_execution_failed", "message": "Research failed."},
                dedupe_key="run:terminal",
            )
        except Exception:
            pass
    finally:
        control_task.cancel()
        await asyncio.gather(control_task, return_exceptions=True)
        await _release_gateway_run(record, config or {})
        _schedule_run_eviction(record, config)


async def _run_control_listener(record: RunRecord, config: dict[str, Any]) -> None:
    """Consume durable commands for the worker that owns a live run."""
    configurable = Configuration.from_runnable_config(config)
    store = RunControlStore(record.run_id, runs_dir=configurable.runs_dir)
    publisher = event_publisher_from_config(config)
    poll_seconds = configurable.sse_poll_interval_ms / 1000
    while True:
        for command in await store.pending():
            try:
                if command.type == "cancel":
                    record.engine.interrupt()
                    record.status = "cancelling"
                elif command.type == "human_action":
                    record.engine.handle_human_action(
                        str(command.payload.get("action_id", "")),
                        str(command.payload.get("action", "")),
                        str(command.payload.get("message", "")),
                    )
                elif command.type == "feedback":
                    await record.engine.submit_feedback(dict(command.payload))
                await store.ack(command)
            except Exception:
                await publisher.publish(
                    "system.warning",
                    payload={
                        "warning_code": "control_command_rejected",
                        "message": "A run control command could not be applied.",
                    },
                    dedupe_key=f"control:{command.command_id}:rejected",
                )
        await asyncio.sleep(poll_seconds)


@app.get("/capabilities")
async def get_capabilities(
    user: Principal = Depends(require_active_user),
) -> dict[str, Any]:
    """Return the safe, explicit browser-editable runtime contract."""
    schema = Configuration.model_json_schema()
    properties = schema.get("properties", {})
    selected = {
        key: properties[key]
        for key in FRONTEND_EDITABLE_CONFIG_KEYS
        if key in properties
    }
    defaults = Configuration.from_runnable_config(None).model_dump(mode="json")
    document_state = await document_health()
    document_settings = get_document_settings()
    publisher_settings = get_publisher_settings()
    return {
        "public_event_schema_version": PUBLIC_EVENT_SCHEMA_VERSION,
        "public_task_activity_schema_version": PUBLIC_TASK_ACTIVITY_SCHEMA_VERSION,
        "publication_schema_version": PUBLICATION_SCHEMA_VERSION,
        "publication_event_schema_version": PUBLICATION_EVENT_SCHEMA_VERSION,
        "accepted_event_schema_versions": [1, PUBLIC_EVENT_SCHEMA_VERSION],
        "features": {
            "clarification": True,
            "human_in_loop": True,
            "feedback": True,
            "artifacts": True,
            "memory": True,
            "subagent_activity": True,
            "subagent_activity_preview": _task_activity_preview_allowed(user),
            "report_review": bool(defaults.get("report_review_enabled", False)),
            "local_dev_auth_bypass": os.environ.get("LOCAL_DEV_AUTH_BYPASS", "").lower()
            in {"1", "true", "yes"},
            "document_research": document_state,
            "publications": {
                "enabled": publisher_settings.enabled,
                "worker": (
                    "ready" if worker_available(publisher_settings) else "degraded"
                ),
            },
        },
        "publication_formats": sorted(PUBLICATION_FORMATS),
        "publication_theme_schema": PublisherTheme.model_json_schema(),
        "publication_theme_defaults": PublisherTheme().model_dump(mode="json"),
        "source_modes": ["web", "documents", "hybrid", "specific"],
        "documents": {
            "formats": ["pdf", "docx", "xlsx", "pptx", "csv", "md", "txt", "png", "jpg", "jpeg", "tif", "tiff"],
            "max_file_bytes": document_settings.max_file_bytes,
            "max_documents_per_user": document_settings.max_documents_per_user,
            "max_bytes_per_user": document_settings.max_bytes_per_user,
            "ocr_enabled": document_settings.ocr_enabled,
            "ocr_mode": document_settings.ocr_mode,
            "ocr_configured": (
                document_settings.ocr_mode == "local"
                or document_settings.remote_ocr_configured
            ),
        },
        "editable_config_keys": list(selected),
        "config_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": selected,
            "$defs": schema.get("$defs", {}),
        },
        "defaults": {key: defaults[key] for key in selected if key in defaults},
        "ui": {
            key: (Configuration.model_fields[key].json_schema_extra or {})
            for key in selected
        },
    }


_MODEL_ROLE_FIELDS = (
    "supervisor_model",
    "research_model",
    "summarization_model",
    "message_summary_model",
    "web_rerank_model",
    "web_evidence_model",
    "compression_model",
    "final_report_model",
    "quality_evaluation_model",
    "report_review_model",
)
_MODEL_CATALOG_CACHE_TTL_SECONDS = 60.0
_model_catalog_cache: dict[str, Any] | None = None
_model_catalog_cache_lock = asyncio.Lock()


def _model_catalog_role_aliases(configurable: Configuration) -> dict[str, str]:
    """Project current role -> gateway alias mappings for the settings UI."""
    aliases: dict[str, str] = {}
    for field_name in _MODEL_ROLE_FIELDS:
        value = getattr(configurable, field_name, None)
        if field_name == "report_review_model" and not value:
            value = getattr(configurable, "quality_evaluation_model", None)
        if value:
            aliases[field_name] = str(value)
    return aliases


async def _load_model_catalog_payload() -> dict[str, Any]:
    """Fetch the LiteLLM catalog behind a small process-level TTL cache.

    Gateway failures never raise: a stale-but-usable snapshot wins over an
    empty response, and a cold cache degrades to an empty model list so the
    settings UI can fall back to free-text input.
    """
    global _model_catalog_cache
    now = time.time()
    cached = _model_catalog_cache
    if cached is not None and now - cached["loaded_at"] < _MODEL_CATALOG_CACHE_TTL_SECONDS:
        return {**cached["payload"], "stale": False}
    async with _model_catalog_cache_lock:
        cached = _model_catalog_cache
        if (
            cached is not None
            and time.time() - cached["loaded_at"] < _MODEL_CATALOG_CACHE_TTL_SECONDS
        ):
            return {**cached["payload"], "stale": False}
        try:
            settings = RunKeySettings.from_env()
        except LiteLLMKeyConfigurationError:
            if cached is not None:
                return {**cached["payload"], "stale": True}
            return {"models": [], "error": "gateway_not_configured", "stale": True}
        client = LiteLLMModelCatalogClient(
            base_url=settings.base_url,
            api_key=settings.master_key,
        )
        try:
            catalog = await client.load()
        except (httpx.HTTPError, ModelCatalogError):
            if cached is not None:
                return {**cached["payload"], "stale": True}
            return {"models": [], "error": "gateway_unavailable", "stale": True}
        finally:
            await client.aclose()
        models = [
            {
                "name": entry.model_name,
                "base_model": entry.base_model,
                "context_window": entry.context_window,
                "max_output_tokens": entry.max_output_tokens,
                "input_cost_per_token": entry.input_cost_per_token,
                "output_cost_per_token": entry.output_cost_per_token,
            }
            for entry in sorted(catalog.values(), key=lambda item: item.model_name)
        ]
        payload: dict[str, Any] = {"models": models, "error": None}
        _model_catalog_cache = {"loaded_at": time.time(), "payload": payload}
        return {**payload, "stale": False}


@app.get("/models")
async def get_model_catalog(
    user: Principal = Depends(require_active_user),
) -> dict[str, Any]:
    """Expose the gateway model catalog so settings can render safe dropdowns."""
    configurable = Configuration.from_runnable_config(None)
    response: dict[str, Any] = {
        "backend": str(configurable.model_backend),
        "role_aliases": _model_catalog_role_aliases(configurable),
    }
    if configurable.model_backend != "litellm":
        response.update({"models": [], "error": None, "stale": False})
        return response
    response.update(await _load_model_catalog_payload())
    return response


@app.get("/runs")
async def list_runs(
    limit: int = 30,
    cursor: str | None = None,
    status: str | None = None,
    user: Principal = Depends(require_permissions(RESEARCH_RUN_READ_OWN.code)),
) -> dict[str, Any]:
    """List the authenticated user's persisted run manifests newest first."""
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit_must_be_between_1_and_100")
    owner = _user_identity(user)
    manifests = [
        item
        for item in _load_manifests()
        if item.owner_id == owner
        and (status is None or item.status == status)
    ]
    manifests.sort(key=lambda item: (item.created_at, item.run_id), reverse=True)
    if cursor:
        cursor_key = _decode_cursor(cursor)
        manifests = [
            item for item in manifests if (item.created_at, item.run_id) < cursor_key
        ]
    page = manifests[: limit + 1]
    has_more = len(page) > limit
    page = page[:limit]
    items = [
        {
            "run_id": item.run_id,
            "title": item.title or item.run_id,
            "query_preview": item.query_preview or item.run_id,
            "status": item.status,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
            "last_event_id": item.last_public_event_seq,
        }
        for item in page
    ]
    next_cursor = (
        _encode_cursor(page[-1].created_at, page[-1].run_id)
        if has_more and page
        else None
    )
    return {"items": items, "next_cursor": next_cursor}


@app.post("/runs/stream")
async def stream_run(
    request: RunRequest,
    user: Principal = Depends(require_permissions(RESEARCH_RUN_CREATE.code)),
) -> StreamingResponse:
    """Run a research request and stream events with SSE."""
    selected_documents = await _validate_run_sources(request, user)
    config = _config_from_request(request, user)
    configurable = Configuration.from_runnable_config(config)
    _enforce_run_create_limits(user, configurable)
    release_token = await _reserve_sse_connection(user, configurable)
    bound_run_id: str | None = None
    try:
        engine = QueryEngine(config)
        record = _new_run_record(
            run_id=engine.run_id,
            engine=engine,
            status="running",
            config=config,
        )
        await bind_run_sources(record.run_id, user.user_id, selected_documents)
        bound_run_id = record.run_id
        if engine.context_store is not None:
            engine.context_store.initialize(_user_identity(user), config)
            engine.context_store._update_manifest(  # noqa: SLF001
                title=_run_title(request, engine.run_id),
                query_preview=_request_query_preview(request),
                publication_theme=(
                    request.publication_theme or PublisherTheme()
                ).model_dump(mode="json"),
            )
        await event_publisher_from_config(config).publish(
            "run.created",
            payload={"status": "pending"},
            dedupe_key="run:created",
        )
        record.task = asyncio.create_task(_run_background(record, request, config))
        _remember_run(record, config)
        logger.info(
            "run created",
            extra={
                "actor": _user_identity(user),
                "action": "run.created",
                "run_id": record.run_id,
            },
        )
        store = RunEventStore(record.run_id, runs_dir=configurable.runs_dir)
    except Exception:
        if bound_run_id is not None:
            with contextlib.suppress(Exception):
                await release_run_sources(bound_run_id)
        await _sse_connection_limiter.release(release_token)
        raise
    return StreamingResponse(
        _limited_sse(
            _public_event_iterator(store, principal=user),
            release_token,
        ),
        media_type="text/event-stream",
        headers=_sse_headers(),
    )


@app.post("/runs")
async def create_run(
    request: RunRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: Principal = Depends(require_permissions(RESEARCH_RUN_CREATE.code)),
) -> dict[str, Any]:
    """Create a background research run."""
    selected_documents = await _validate_run_sources(request, user)
    config = _config_from_request(request, user)
    if idempotency_key:
        existing = _find_idempotent_run(config, _user_identity(user), idempotency_key)
        if existing is not None:
            return {
                "run_id": existing.run_id,
                "status": existing.status,
                "events_url": f"/runs/{existing.run_id}/events",
                "last_event_id": existing.last_public_event_seq,
                "idempotent_replay": True,
            }
    _enforce_run_create_limits(user, Configuration.from_runnable_config(config))
    engine = QueryEngine(config)
    record = _new_run_record(
        run_id=engine.run_id,
        engine=engine,
        status="running",
        config=config,
    )
    bound = False
    try:
        await bind_run_sources(record.run_id, user.user_id, selected_documents)
        bound = True
        if engine.context_store is not None:
            engine.context_store.initialize(_user_identity(user), config)
            engine.context_store._update_manifest(  # noqa: SLF001
                title=_run_title(request, engine.run_id),
                query_preview=_request_query_preview(request),
                idempotency_key=idempotency_key,
                publication_theme=(
                    request.publication_theme or PublisherTheme()
                ).model_dump(mode="json"),
            )
        created = await event_publisher_from_config(config).publish(
            "run.created",
            payload={"status": "pending"},
            dedupe_key="run:created",
        )
        record.task = asyncio.create_task(_run_background(record, request, config))
        _remember_run(record, config)
    except Exception:
        if bound:
            with contextlib.suppress(Exception):
                await release_run_sources(record.run_id)
        raise
    logger.info(
        "run created",
        extra={"actor": _user_identity(user), "action": "run.created", "run_id": record.run_id},
    )
    return {
        "run_id": record.run_id,
        "status": record.status,
        "events_url": f"/runs/{record.run_id}/events",
        "last_event_id": created.sequence,
        "idempotent_replay": False,
    }


def _publication_context(
    run_id: str,
    user: Principal,
) -> tuple[Configuration, Any, PublicationJobStore]:
    """Authorize a run and return its durable publication context."""
    record, configurable = _require_run_owner(run_id, user)
    context = (
        getattr(record.engine, "context_store", None)
        if record is not None
        else None
    )
    if context is None:
        context = RunContextStore(run_id, runs_dir=configurable.runs_dir)
    try:
        manifest = context.load_manifest()
    except (JournalCorruptedError, OSError, ValueError, portalocker.exceptions.LockException):
        raise HTTPException(
            status_code=409,
            detail="publication_requires_persisted_run",
        ) from None
    return (
        configurable,
        manifest,
        PublicationJobStore(run_id, runs_dir=configurable.runs_dir),
    )


def _default_publication_theme(manifest: Any) -> PublisherTheme:
    """Load a persisted run theme, falling back to the bounded defaults."""
    try:
        return PublisherTheme.model_validate(manifest.publication_theme or {})
    except Exception:
        return PublisherTheme()


def _publication_job_or_404(
    store: PublicationJobStore,
    publication_id: str,
) -> PublicationJob:
    try:
        job = store.get(publication_id)
    except portalocker.exceptions.LockException:
        raise HTTPException(status_code=503, detail="publication_store_busy") from None
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="publication_not_found") from None
    if job is None:
        raise HTTPException(status_code=404, detail="publication_not_found")
    return job


@app.post("/runs/{run_id}/publications")
async def create_publication(
    run_id: str,
    request: PublicationRequest,
    user: Principal = Depends(require_permissions(RESEARCH_RUN_READ_OWN.code)),
) -> Response:
    """Queue one owner-scoped report publication without changing Run status."""
    settings = get_publisher_settings()
    configurable, manifest, store = _publication_context(run_id, user)
    if not settings.enabled:
        raise HTTPException(status_code=503, detail="publisher_disabled")
    if manifest.status not in {"completed", "success"}:
        raise HTTPException(status_code=409, detail="run_not_completed")
    try:
        markdown = store.load_final_report()
    except (OSError, ValueError, portalocker.exceptions.LockException):
        raise HTTPException(status_code=409, detail="final_report_unavailable") from None
    if not markdown:
        raise HTTPException(status_code=409, detail="final_report_unavailable")
    if len(markdown) > settings.max_input_chars:
        raise HTTPException(status_code=413, detail="publication_input_too_large")
    report_sha256 = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    theme = request.theme or _default_publication_theme(manifest)

    def append_queued_event(job: PublicationJob) -> None:
        """Append the queue event while the Job lock is still held."""
        try:
            PublicationEventStore(
                run_id,
                runs_dir=configurable.runs_dir,
            ).append(
                "publication.queued",
                publication_id=job.publication_id,
                payload=publication_event_payload(job),
                dedupe_key=f"{job.publication_id}:publication.queued:0",
            )
        except Exception as exc:  # job status remains authoritative
            logger.warning(
                "publication queued event failed run_id=%s publication_id=%s error_type=%s",
                run_id,
                job.publication_id,
                type(exc).__name__,
            )

    try:
        job, created = await asyncio.to_thread(
            store.enqueue,
            report_sha256=report_sha256,
            publication_format=request.format,
            theme=theme,
            max_attempts=settings.max_attempts,
            on_created=append_queued_event,
        )
    except (OSError, portalocker.exceptions.LockException):
        raise HTTPException(status_code=503, detail="publication_store_busy") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    response = _publication_response(job)
    response["reused"] = not created
    status_code = 202 if job.status in {"queued", "running"} else 200
    return JSONResponse(response, status_code=status_code)


@app.get("/runs/{run_id}/publications")
async def list_publications(
    run_id: str,
    user: Principal = Depends(require_permissions(RESEARCH_RUN_READ_OWN.code)),
) -> dict[str, Any]:
    """List durable publication jobs for one owned run."""
    _configurable, _manifest, store = _publication_context(run_id, user)
    jobs = await asyncio.to_thread(store.list)
    default_theme = _default_publication_theme(_manifest)
    return {
        "run_id": run_id,
        "items": [_publication_response(job) for job in jobs[:100]],
        "events_url": f"/runs/{run_id}/publications/events",
        "default_theme": default_theme.model_dump(mode="json"),
        "worker": (
            "ready"
            if worker_available(get_publisher_settings())
            else "degraded"
        ),
    }


@app.get("/runs/{run_id}/publications/events")
async def stream_publication_events(
    run_id: str,
    after: int = 0,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    user: Principal = Depends(require_permissions(RESEARCH_RUN_READ_OWN.code)),
) -> StreamingResponse:
    """Replay and tail the independent publication lifecycle stream."""
    configurable, _manifest, _job_store = _publication_context(run_id, user)
    store = PublicationEventStore(run_id, runs_dir=configurable.runs_dir)
    cursor = after
    if last_event_id is not None:
        try:
            cursor = int(last_event_id)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="invalid_publication_event_cursor",
            ) from None
    if cursor < 0:
        raise HTTPException(
            status_code=400,
            detail="invalid_publication_event_cursor",
        )
    try:
        current = await asyncio.to_thread(store.last_sequence)
    except (OSError, ValueError, portalocker.exceptions.LockException):
        raise HTTPException(status_code=503, detail="publication_store_busy") from None
    if cursor > current:
        raise HTTPException(status_code=409, detail="publication_event_cursor_ahead")
    release_token = await _reserve_sse_connection(user, configurable)
    return StreamingResponse(
        _limited_sse(
            _publication_event_iterator(store, after=cursor, principal=user),
            release_token,
        ),
        media_type="text/event-stream",
        headers=_sse_headers(),
    )


@app.get("/runs/{run_id}/publications/{publication_id}")
async def publication_status(
    run_id: str,
    publication_id: str,
    user: Principal = Depends(require_permissions(RESEARCH_RUN_READ_OWN.code)),
) -> dict[str, Any]:
    """Return one publication job without exposing its storage path."""
    _configurable, _manifest, store = _publication_context(run_id, user)
    return _publication_response(_publication_job_or_404(store, publication_id))


@app.post("/runs/{run_id}/publications/{publication_id}/retry")
async def retry_publication(
    run_id: str,
    publication_id: str,
    user: Principal = Depends(require_permissions(RESEARCH_RUN_READ_OWN.code)),
) -> Response:
    """Requeue a retryable failed publication."""
    configurable, _manifest, store = _publication_context(run_id, user)
    _publication_job_or_404(store, publication_id)

    def append_requeued_event(job: PublicationJob) -> None:
        """Append the requeue event before another worker can claim the Job."""
        try:
            PublicationEventStore(
                run_id,
                runs_dir=configurable.runs_dir,
            ).append(
                "publication.requeued",
                publication_id=job.publication_id,
                payload=publication_event_payload(job),
                dedupe_key=f"{job.publication_id}:manual-requeue:{job.attempt}",
            )
        except Exception:
            pass

    try:
        job = await asyncio.to_thread(
            store.retry,
            publication_id,
            on_requeued=append_requeued_event,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="publication_not_found") from None
    except (OSError, portalocker.exceptions.LockException):
        raise HTTPException(status_code=503, detail="publication_store_busy") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(_publication_response(job), status_code=202)


@app.get("/runs/{run_id}/publications/{publication_id}/download")
async def download_publication(
    run_id: str,
    publication_id: str,
    user: Principal = Depends(require_permissions(RESEARCH_RUN_READ_OWN.code)),
) -> FileResponse:
    """Download one completed, hash-verified publication."""
    _configurable, _manifest, store = _publication_context(run_id, user)
    job = _publication_job_or_404(store, publication_id)
    if job.status != "completed" or job.artifact is None:
        raise HTTPException(status_code=409, detail="publication_not_ready")
    try:
        path = await asyncio.to_thread(store.artifact_path, job, verify=True)
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail="publication_artifact_missing",
        ) from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (OSError, portalocker.exceptions.LockException):
        raise HTTPException(status_code=503, detail="publication_store_busy") from None
    return FileResponse(
        path,
        media_type=job.artifact.media_type,
        filename=job.artifact.filename,
        headers={
            "ETag": f'"{job.artifact.sha256}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


@app.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    user: Principal = Depends(require_permissions(RESEARCH_RUN_READ_OWN.code)),
) -> dict[str, Any]:
    """Return the latest status/result for a run."""
    record = _runs.get(run_id)
    if record is None:
        configurable = Configuration.from_runnable_config(None)
        try:
            store = RunContextStore(run_id, runs_dir=configurable.runs_dir)
            manifest = store.load_manifest()
        except (ValueError, JournalCorruptedError, OSError):
            raise HTTPException(status_code=404, detail="Run not found") from None
        if not manifest.owner_id or manifest.owner_id != user.user_id:
            raise HTTPException(status_code=404, detail="Run not found")
        result = manifest.result
        report_text = ""
        if manifest.status in {"completed", "success"}:
            report_path = store.context_dir / "final_report.md"
            if report_path.exists():
                report_text = report_path.read_text(encoding="utf-8")
                # Preserve a partial completion status and quality metadata
                # from the durable manifest while restoring the Markdown body.
                restored_result = dict(result or {}) if isinstance(result, dict) else {}
                restored_result.setdefault("status", "success")
                restored_result["result"] = report_text
                result = restored_result
        event_store = RunEventStore(run_id, runs_dir=configurable.runs_dir)
        projection = event_store.project() if event_store.exists else None
        projection = _augment_run_projection(run_id, projection, configurable)
        stored_configurable = (
            dict(manifest.config.get("configurable") or {})
            if isinstance(manifest.config, dict)
            else {}
        )
        stored_configurable = (
            dict(manifest.config.get("configurable") or {})
            if isinstance(manifest.config, dict)
            else {}
        )
        output = _stable_output(
            manifest.result,
            report_text,
            publications=await _run_publications(run_id, configurable.runs_dir),
            preferred_output_format=stored_configurable.get("output_format"),
            publication_theme=manifest.publication_theme,
        )
        if output.get("report_review") is None and projection is not None:
            output["report_review"] = projection.report_review or None
        return {
            "run_id": run_id,
            "title": manifest.title or run_id,
            "status": manifest.status,
            "created_at": manifest.created_at,
            "updated_at": manifest.updated_at,
            "runtime_seconds": max(0.0, manifest.updated_at - manifest.created_at),
            "pending_human_action": manifest.pending_human_action,
            "pending_security_approvals": (
                projection.pending_security_approvals if projection else []
            ),
            "result": result,
            "output": output,
            "event_count": manifest.last_journal_seq,
            "persistence_degraded": manifest.persistence_degraded,
            "progress": projection.model_dump() if projection else None,
            "events_url": f"/runs/{run_id}/events",
            "last_event_id": projection.last_event_id if projection else 0,
        }
    _require_record_owner(record, user)
    configurable = Configuration.from_runnable_config(record.engine.config)
    event_store = RunEventStore(run_id, runs_dir=configurable.runs_dir)
    projection = event_store.project()
    projection = _augment_run_projection(run_id, projection, configurable)
    manifest = (
        record.engine.context_store.load_manifest()
        if getattr(record.engine, "context_store", None)
        and record.engine.context_store.manifest_path.exists()
        else None
    )
    now = time.time()
    manifest_status = manifest.status if manifest is not None else None
    if (
        manifest_status in {"completed", "failed", "cancelled", "interrupted"}
        and record.status != manifest_status
    ):
        # The durable manifest is authoritative once terminal; the in-memory
        # record can lag behind a just-finished run.
        run_status = manifest_status
    else:
        run_status = record.status
    # The engine publishes the terminal SSE event before stream consumption
    # copies final_state into RunRecord. Serve that already-committed result
    # even when this request races the background consumer (also on resume).
    result = record.result
    if getattr(record.engine, "status", None) in {"completed", "failed", "cancelled"}:
        result = getattr(record.engine, "final_state", None) or result
        run_status = record.engine.status
    output = _stable_output(
        result,
        publications=await _run_publications(run_id, configurable.runs_dir),
        preferred_output_format=getattr(configurable, "output_format", None),
        publication_theme=(
            manifest.publication_theme
            if manifest is not None
            else dict(
                getattr(record.engine, "config", {})
                .get("metadata", {})
                .get("publication_theme")
                or {}
            )
        ),
    )
    if output.get("report_review") is None and projection.report_review:
        output["report_review"] = projection.report_review
    return {
        "run_id": run_id,
        "title": (manifest.title if manifest else None) or run_id,
        "status": run_status,
        "created_at": manifest.created_at if manifest else record.engine.started_at,
        "updated_at": manifest.updated_at if manifest else now,
        "runtime_seconds": max(0.0, now - record.engine.started_at),
        "pending_human_action": (
            getattr(record.engine, "pending_human_action", None)
            or (manifest.pending_human_action if manifest else None)
            or projection.pending_human_action
        ),
        "pending_security_approvals": projection.pending_security_approvals,
        "result": result,
        "output": output,
        "event_count": projection.last_event_id,
        "progress": projection.model_dump(),
        "events_url": f"/runs/{run_id}/events",
        "last_event_id": projection.last_event_id,
    }


@app.post("/runs/{run_id}/resume", status_code=202)
async def resume_run(
    run_id: str,
    request: ResumeRunRequest,
    user: Principal = Depends(require_permissions(RESEARCH_RUN_CONTROL_OWN.code)),
) -> dict[str, str]:
    """Explicitly resume an interrupted file-backed Query run."""
    active = _runs.get(run_id)
    if active is not None:
        _require_record_owner(active, user)
        if active.status == "completed":
            raise HTTPException(status_code=409, detail="run_already_completed")
        if active.status == "cancelled":
            raise HTTPException(status_code=409, detail="run_not_recoverable")
        if active.status not in {"failed", "cancelled"}:
            raise HTTPException(status_code=409, detail="run_already_active")

    try:
        validate_http_configurable(request.configurable)
        validate_http_metadata(request.metadata)
    except ValueError as exc:
        logger.warning("security.unsafe_config_rejected: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    config = apply_principal_to_config(
        {
            "configurable": dict(request.configurable),
            "metadata": {
                **request.metadata,
                "run_id": run_id,
                "deployment_surface": "http",
                "request_id": current_request_id(),
            },
        },
        user,
    )
    runs_dir = str(request.configurable.get("runs_dir") or Configuration.from_runnable_config(None).runs_dir)
    try:
        engine = QueryEngine.load(run_id, runs_dir=runs_dir, config=config)
        if engine.context_store is None:
            raise JournalCorruptedError("run_not_recoverable")
        replay = engine.context_store.replay()
    except RunContextError as exc:
        if str(exc).startswith("run_schema_not_resumable:"):
            raise HTTPException(
                status_code=409,
                detail=str(exc),
            ) from None
        raise HTTPException(status_code=409, detail="run_not_recoverable") from None
    except (ValueError, OSError):
        raise HTTPException(status_code=409, detail="run_not_recoverable") from None
    if not replay.manifest.owner_id or replay.manifest.owner_id != user.user_id:
        raise HTTPException(status_code=404, detail="Run not found")
    if replay.manifest.status == "completed":
        raise HTTPException(status_code=409, detail="run_already_completed")
    if replay.manifest.status == "cancelled" or replay.manifest.next_stage == "cancelled":
        raise HTTPException(status_code=409, detail="run_not_recoverable")
    try:
        await engine.acquire_run_lease()
    except Exception as exc:
        from open_deep_research.tasks.lease import LeaseConflictError

        if isinstance(exc, LeaseConflictError):
            raise HTTPException(status_code=409, detail="run_already_active") from None
        raise

    effective_config = getattr(engine, "config", config)
    record = _new_run_record(
        run_id=run_id,
        engine=engine,
        status="running",
        config=effective_config,
    )
    record.task = asyncio.create_task(_run_resumed_background(record))
    _remember_run(record, effective_config)
    logger.info(
        "run resumed",
        extra={"actor": _user_identity(user), "action": "run.resumed", "run_id": run_id},
    )
    return {"run_id": run_id, "status": "running"}


@app.post("/runs/{run_id}/human-actions/{action_id}")
async def submit_human_action(
    run_id: str,
    action_id: str,
    request: HumanActionRequest,
    user: Principal = Depends(require_permissions(RESEARCH_RUN_INTERACT_OWN.code)),
) -> dict[str, Any]:
    """Resolve a pending human approval, revision, or cancellation action."""
    record, configurable = _require_run_owner(run_id, user)
    if record is not None:
        try:
            result = record.engine.handle_human_action(action_id, request.action, request.message or "")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if request.action == "cancel":
            record.status = "cancelled"
        return result
    manifest = RunContextStore(run_id, runs_dir=configurable.runs_dir).load_manifest()
    pending = manifest.pending_human_action or {}
    if pending.get("action_id") != action_id:
        raise HTTPException(status_code=400, detail="No matching pending human action")
    allowed = (
        {"answer", "cancel"}
        if pending.get("type") == "clarification"
        else {"approve", "deny", "cancel"}
        if pending.get("type") == "fetch_budget_approval"
        else {"approve", "revise", "cancel"}
    )
    if request.action not in allowed:
        raise HTTPException(
            status_code=400,
            detail="Human action does not match the pending action type",
        )
    if request.action in {"answer", "revise"} and not (request.message or "").strip():
        raise HTTPException(status_code=400, detail="A message is required for this human action")
    command = await RunControlStore(run_id, runs_dir=configurable.runs_dir).enqueue(
        "human_action",
        {"action_id": action_id, "action": request.action, "message": request.message or ""},
        command_id=f"human-action-{action_id}",
    )
    return {"status": "accepted", "command_id": command.command_id, "action": request.action}


async def _sandbox_store_context(
    run_id: str,
    *,
    require_live_fence: bool = False,
) -> tuple[Configuration, int, dict[str, Any]]:
    """Resolve store configuration and current fence after RBAC authorization."""
    if _native_research_service is not None:
        from open_deep_research.agentscope_runtime.native_security import sandbox_context
        native = await sandbox_context(_native_research_service, run_id, require_live_fence=require_live_fence)
        if native is not None:
            return native
    record = _runs.get(run_id)
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


@app.get("/runs/{run_id}/security-approvals")
async def list_security_approvals(
    run_id: str,
    status: Literal["pending", "resolved", "expired", "consumed"] | None = "pending",
    user: Principal = Depends(
        require_run_owner_or_any(
            RESEARCH_SECURITY_APPROVAL_READ_OWN.code,
            RESEARCH_SECURITY_APPROVAL_READ_ANY.code,
        )
    ),
) -> dict[str, Any]:
    """List the caller-authorized run's durable sandbox approval queue."""
    del user
    configurable, _fence_token, _config = await _sandbox_store_context(run_id)
    version, approvals = await asyncio.to_thread(
        SecurityApprovalStore(run_id, runs_dir=configurable.runs_dir).list,
        status=status,
    )
    return {
        "run_id": run_id,
        "version": version,
        "approvals": [approval.model_dump(mode="json") for approval in approvals],
    }


@app.post("/runs/{run_id}/security-approvals/{approval_id}")
async def resolve_security_approval(
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
    configurable, fence_token, config = await _sandbox_store_context(
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
        raise HTTPException(status_code=404, detail="security_approval_not_found") from exc
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
        task_pending = [item for item in pending if item.task_id == approval.task_id]
        if task_pending:
            task.pending_domain = str(
                task_pending[0].target.get("domain") or ""
            ) or None
            task.pending_domain_tool = task_pending[0].capability
        else:
            task.pending_domain = None
            task.pending_domain_tool = None
        if (
            not task_pending
            and task.status == TaskStatus.WAITING_FOR_CONFIRMATION
        ):
            get_task_registry().update_status(approval.task_id, TaskStatus.RUNNING)
    return approval.model_dump(mode="json")


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
    run_setting = getattr(
        configurable, "sandbox_egress_approval_mode", "profile"
    )
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




def _read_egress_state(run_id: str, configurable: Configuration, fence_token: int) -> dict[str, Any]:
    mode = _egress_mode_state(configurable, run_id=run_id, fence_token=fence_token,
        override_mode=_runtime_egress_override(run_id, configurable.runs_dir, fence_token=fence_token))
    store = SecurityApprovalStore(run_id, runs_dir=configurable.runs_dir)
    target_state = store.target_state(fence_token)
    _, approvals = store.list()
    ledger = EgressClassificationStore(run_id, runs_dir=configurable.runs_dir)
    try:
        classifications = list(ledger.load().values())
        health = ledger.classifier_state()
    except (OSError, ValueError, JournalCorruptedError):
        # Manual decisions remain available when the auxiliary classifier
        # ledger is unreadable. Do not invent zero consumed model budget.
        classifications = []
        health = {"degraded": True, "reason": "state_unavailable"}
    _, _, profile = resolve_profile(configurable)
    for item in target_state["targets"]:
        host, port = item["target"]["domain"], item["target"]["port"]
        item["policy_denied"] = network_target_decision(profile.network, host, port) == "deny"
        matching = [entry for entry in classifications if entry.get("host") == host
                    and entry.get("port") == port and entry.get("capability") == item["capability"]
                    and str(entry.get("fingerprint", "")).startswith("egress:v2:")]
        item["classification"] = max(matching, key=lambda e: e.get("classified_at", 0), default=None)
    limit = configurable.egress_classifier_max_calls_per_run
    health.update(max_calls=limit)
    if health.get("reason") != "state_unavailable":
        health["remaining_calls"] = max(0, limit - int(health.get("calls_used", 0)))
    allowed_modes = [candidate for candidate in ("manual", "auto", "open")
        if (resolved := effective_egress_mode(mode["baseline_mode"], mode["run_setting"], candidate)).mode == candidate
        and not resolved.capped]
    return {**mode, **target_state, "allowed_modes": allowed_modes, "health": health,
            "records": [item.model_dump(mode="json") for item in approvals if item.fence_token == fence_token],
            "classifications": classifications[-200:]}


@app.get("/runs/{run_id}/egress-state")
async def get_egress_state(run_id: str, user: Principal = Depends(require_run_owner_or_any(
    RESEARCH_SECURITY_APPROVAL_READ_OWN.code, RESEARCH_SECURITY_APPROVAL_READ_ANY.code,
))) -> dict[str, Any]:
    """Return a reloadable snapshot of permissions, decisions, and health."""
    configurable, fence_token, _ = await _sandbox_store_context(run_id)
    result = await asyncio.to_thread(_read_egress_state, run_id, configurable, fence_token)
    owns_run = await _rbac_run_owner_checker(None, user, run_id)
    result["can_resolve"] = user.has_any([RESEARCH_SECURITY_APPROVAL_RESOLVE_ANY.code]) or (
        owns_run and user.has_any([RESEARCH_SECURITY_APPROVAL_RESOLVE_OWN.code]))
    result["can_interact"] = owns_run and user.has_any([RESEARCH_RUN_INTERACT_OWN.code])
    return result


@app.post("/runs/{run_id}/egress-targets/{target_id}/decision")
async def decide_egress_target(run_id: str, target_id: str, request: EgressTargetDecisionRequest,
    user: Principal = Depends(require_run_owner_or_any(
        RESEARCH_SECURITY_APPROVAL_RESOLVE_OWN.code, RESEARCH_SECURITY_APPROVAL_RESOLVE_ANY.code,
    )),
) -> dict[str, Any]:
    """Apply an exact target override, never overriding administrator denial."""
    configurable, fence_token, config = await _sandbox_store_context(run_id, require_live_fence=True)
    store = SecurityApprovalStore(run_id, runs_dir=configurable.runs_dir)
    snapshot = await asyncio.to_thread(store.target_state, fence_token)
    target = next((item for item in snapshot["targets"] if item["target_id"] == target_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="egress_target_not_found")
    _, _, profile = resolve_profile(configurable)
    if request.decision == "allow_run" and network_target_decision(
        profile.network, target["target"]["domain"], target["target"]["port"]
    ) == "deny":
        raise HTTPException(status_code=403, detail="egress_target_policy_denied")
    try:
        result = await asyncio.to_thread(store.decide_target, target_id, decision=request.decision,
            reason=request.reason, actor=user.user_id, expected_version=request.expected_version,
            fence_token=fence_token)
    except ValueError as exc:
        latest = await asyncio.to_thread(store.target_state, fence_token)
        raise HTTPException(status_code=409, detail={"code": str(exc), "state": latest}) from exc
    await event_publisher_from_config(config).publish("security.egress_target_changed",
        stage="researching", payload=result,
        dedupe_key=f"egress-target:{target_id}:{fence_token}:{result['version']}")
    return result


@app.get("/runs/{run_id}/egress-mode")
async def get_run_egress_mode(
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
    configurable, fence_token, _config = await _sandbox_store_context(run_id)
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


@app.post("/runs/{run_id}/egress-mode")
async def set_run_egress_mode(
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
    configurable, fence_token, config = await _sandbox_store_context(
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


@app.post("/runs/{run_id}/feedback")
async def submit_feedback(
    run_id: str,
    request: HumanFeedbackRequest,
    user: Principal = Depends(require_permissions(RESEARCH_RUN_INTERACT_OWN.code)),
) -> dict[str, Any]:
    """Accept mid-run human direction or evidence questions."""
    record, configurable = _require_run_owner(run_id, user)
    payload = request.model_dump(exclude_none=True)
    if record is not None:
        try:
            return await record.engine.submit_feedback(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    command = await RunControlStore(run_id, runs_dir=configurable.runs_dir).enqueue(
        "feedback",
        payload,
        command_id=request.command_id,
    )
    return {"status": "accepted", "command_id": command.command_id}


@app.get("/runs/{run_id}/team")
async def get_research_team(
    run_id: str,
    user: Principal = Depends(require_permissions(RESEARCH_RUN_READ_OWN.code)),
) -> dict[str, Any]:
    """Return the owned team's shared task list and bounded message history."""
    _record, configurable = _require_run_owner(run_id, user)
    if not configurable.enable_async_research:
        return {"enabled": False}
    from open_deep_research.tasks.team_runtime import team_runtime
    service = await team_runtime.start()
    async with service.store.pool.acquire() as db:
        team = await db.fetchrow("SELECT name,status FROM research_teams WHERE run_id=$1", run_id)
        if team is None:
            return {"enabled": True, "members": [], "tasks": [], "messages": []}
        members = await db.fetch("SELECT member_id,name,purpose,status FROM research_team_members WHERE run_id=$1 ORDER BY name", run_id)
        messages = await db.fetch("""SELECT event FROM research_coordination_events
            WHERE run_id=$1 AND event->>'type'='message' ORDER BY sequence DESC LIMIT 50""", run_id)
    tasks = await service.tasks(run_id)
    keys = {"task_id", "display_title", "status", "owner", "admission_status", "blocked_by", "error_message"}
    return {"enabled": True, **dict(team), "members": [dict(row) for row in members],
            "tasks": [{key: value for key, value in task.items() if key in keys} for task in tasks],
            "messages": [json.loads(row["event"]) for row in reversed(messages)]}




@app.post("/runs/{run_id}/team/messages")
async def send_team_message(
    run_id: str, request: TeamMessageRequest,
    user: Principal = Depends(require_permissions(RESEARCH_RUN_INTERACT_OWN.code)),
) -> dict[str, Any]:
    """Send human instructions through the same transaction and recipient rules."""
    record, configurable = _require_run_owner(run_id, user)
    if record is None or not configurable.enable_async_research:
        raise HTTPException(status_code=409, detail="team_run_not_active")
    from open_deep_research.tasks.team_protocol import MemberIdentity
    from open_deep_research.tasks.team_runtime import team_runtime
    service = await team_runtime.start()
    try:
        return await service.command(MemberIdentity(run_id=run_id, member_id="lead", name="lead", role="lead"),
            f"human:{request.command_id}", "message", {"to": request.to, "message": request.message},
            fence_token=int(record.engine.config["metadata"]["run_fence_token"]))
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/runs/{run_id}/tasks/{task_id}/activity")
async def get_task_activity(
    run_id: str,
    task_id: str,
    before: int | None = None,
    limit: int = 100,
    kind: str | None = None,
    user: Principal = Depends(require_permissions(RESEARCH_TASK_ACTIVITY_READ_OWN.code)),
) -> dict[str, Any]:
    """Return one reverse-page of safe task activity in chronological order."""
    _record, configurable = _require_run_owner(run_id, user)
    _require_task_in_run(run_id, task_id, configurable)
    if before is not None and before < 1:
        raise HTTPException(status_code=400, detail="invalid_activity_cursor")
    if not 1 <= limit <= 200:
        raise HTTPException(status_code=422, detail="activity_limit_out_of_range")
    valid_kinds = {
        "lifecycle", "model", "tool", "source", "quality", "checkpoint",
        "control", "security", "error",
    }
    if kind is not None and kind not in valid_kinds:
        raise HTTPException(status_code=400, detail="invalid_activity_kind")

    store = TaskActivityStore(run_id, task_id, runs_dir=configurable.runs_dir)
    source = "native"
    if store.exists:
        items, has_more, last_event_id = await asyncio.to_thread(
            store.page,
            before=before,
            limit=limit,
            kind=kind,
        )
    else:
        observed = _observability_store()
        run = observed.get_run(run_id, user_id=_user_identity(user))
        all_derived = derive_trace_activity(
            run_id,
            task_id,
            observed.list_spans(run_id) if run is not None else [],
        )
        last_event_id = all_derived[-1].sequence if all_derived else 0
        source = "derived_trace" if all_derived else "summary_only"
        derived = all_derived
        if kind is not None:
            derived = [event for event in derived if event.kind == kind]
        if before is not None:
            derived = [event for event in derived if event.sequence < before]
        has_more = len(derived) > limit
        items = derived[-limit:]
    return {
        "run_id": run_id,
        "task_id": task_id,
        "items": [event.public_dict() for event in items],
        "oldest_sequence": items[0].sequence if items else 0,
        "last_event_id": last_event_id,
        "has_more": has_more,
        "detail_level": "preview" if _task_activity_preview_allowed(user) else "summary",
        "source": source,
        "stream_url": f"/runs/{run_id}/tasks/{task_id}/activity/stream",
    }


@app.get("/runs/{run_id}/tasks/{task_id}/activity/stream")
async def stream_task_activity(
    run_id: str,
    task_id: str,
    after: int = 0,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    user: Principal = Depends(require_permissions(RESEARCH_TASK_ACTIVITY_READ_OWN.code)),
) -> StreamingResponse:
    """Replay and tail a task-local activity stream while the drawer is open."""
    record, configurable = _require_run_owner(run_id, user)
    _require_task_in_run(run_id, task_id, configurable)
    store = TaskActivityStore(run_id, task_id, runs_dir=configurable.runs_dir)
    cursor = after
    if last_event_id is not None:
        try:
            cursor = int(last_event_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid_activity_cursor") from None
    if cursor < 0:
        raise HTTPException(status_code=400, detail="invalid_activity_cursor")
    current = await asyncio.to_thread(store.last_sequence)
    if cursor > current:
        raise HTTPException(status_code=409, detail="activity_cursor_ahead")
    if not store.exists and (
        record is None or record.status in {"completed", "failed", "cancelled"}
    ):
        raise HTTPException(status_code=409, detail="activity_stream_unavailable_legacy_run")
    release_token = await _reserve_sse_connection(user, configurable)
    return StreamingResponse(
        _limited_sse(
            _task_activity_iterator(store, after=cursor, principal=user),
            release_token,
        ),
        media_type="text/event-stream",
        headers=_sse_headers(),
    )


@app.get("/runs/{run_id}/events")
async def stream_run_events(
    run_id: str,
    after: int = 0,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    user: Principal = Depends(require_permissions(RESEARCH_RUN_READ_OWN.code)),
) -> StreamingResponse:
    """Replay and tail the durable public event stream for a run."""
    record = _runs.get(run_id)
    if record is not None:
        _require_record_owner(record, user)
        configurable = Configuration.from_runnable_config(record.engine.config)
    else:
        configurable = Configuration.from_runnable_config(None)
        try:
            manifest = RunContextStore(run_id, runs_dir=configurable.runs_dir).load_manifest()
        except (ValueError, JournalCorruptedError, OSError):
            raise HTTPException(status_code=404, detail="Run not found") from None
        if not manifest.owner_id or manifest.owner_id != _user_identity(user):
            raise HTTPException(status_code=404, detail="Run not found")

    store = RunEventStore(run_id, runs_dir=configurable.runs_dir)
    if not store.exists and (record is None or (record.task is not None and record.task.done())):
        raise HTTPException(status_code=409, detail="event_stream_unavailable_legacy_run")
    cursor = after
    if last_event_id is not None:
        try:
            cursor = int(last_event_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid_event_cursor") from None
    if cursor < 0:
        raise HTTPException(status_code=400, detail="invalid_event_cursor")
    current = await asyncio.to_thread(store.last_sequence)
    if cursor > current:
        raise HTTPException(status_code=409, detail="event_cursor_ahead")
    release_token = await _reserve_sse_connection(user, configurable)
    return StreamingResponse(
        _limited_sse(
            _public_event_iterator(store, after=cursor, principal=user),
            release_token,
        ),
        media_type="text/event-stream",
        headers=_sse_headers(),
    )


def _unavailable_usage_response(
    run_id: str,
    *,
    status: str = "unknown",
    configurable: Configuration,
    reason: str = "storage_unavailable",
) -> dict[str, Any]:
    vector = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cached_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "reasoning_tokens": 0,
    }
    limits = {
        "input_tokens": configurable.max_run_input_tokens,
        "output_tokens": configurable.max_run_output_tokens,
        "model_calls": configurable.max_run_model_calls,
        "cost_micro_usd": configurable.max_run_cost_micro_usd,
    }
    return {
        "schema_version": 1,
        "run_id": run_id,
        "status": status,
        "duration_ms": None,
        "revision": 0,
        "updated_at": None,
        "accounting_status": "unavailable",
        "unavailable_reason": reason,
        "totals": {
            "reported": dict(vector),
            "estimated": dict(vector),
            "calls": {
                "attempts": 0,
                "successful_responses": 0,
                "provider_reported": 0,
                "provider_partial": 0,
                "estimated": 0,
                "missing": 0,
                "unknown_failed_attempts": 0,
                "legacy_unclassified": 0,
                "coverage_ratio": 0.0,
            },
            "cost": {
                "estimated_cost_micro_usd": None,
                "cost_source": "unavailable",
                "price_table_hash": None,
            },
            "budgets": {
                key: {
                    "settled": None if key == "cost_micro_usd" else 0,
                    "estimated": 0,
                    "reserved": 0,
                    "limit": limit,
                }
                for key, limit in limits.items()
            },
        },
        "breakdowns": {
            "by_stage": [],
            "by_agent_role": [],
            "by_model": [],
            "by_task": [],
        },
        "timeline": [],
        "operations": {
            "llm_call_count": 0,
            "retry_count": 0,
            "rate_limited_count": 0,
            "rate_429": 0.0,
            "cache_hit_rate": 0.0,
            "cache_input_ratio": 0.0,
            "reasoning_output_ratio": 0.0,
            "output_tokens_per_second": 0.0,
            "tool_call_count": 0,
            "tool_success_rate": 0.0,
            "empty_tool_result_count": 0,
            "zero_source_search_count": 0,
        },
    }


def _outstanding_usage_budget(
    configurable: Configuration,
    run_id: str,
) -> dict[str, int]:
    try:
        return RunBudgetLedger(
            run_id,
            runs_dir=configurable.runs_dir,
        ).outstanding_by_dimension()
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        logger.warning(
            "Token budget ledger unavailable for run %s",
            run_id,
            exc_info=True,
        )
        return {}


def _load_run_usage_response(
    run_id: str,
    *,
    status: str,
    configurable: Configuration,
    duration_ms: int | None = None,
) -> dict[str, Any]:
    if not configurable.token_usage_accounting_enabled:
        response = _unavailable_usage_response(
            run_id,
            status=status,
            configurable=configurable,
            reason="accounting_disabled",
        )
        response["duration_ms"] = duration_ms
        return response
    try:
        store = SQLiteTraceStore(configurable.trace_store_path)
        if configurable.token_usage_accounting_enabled:
            # Runs whose transitions predate the transition-time backfill are
            # healed from their durable Operation Journal on first read.
            reconcile_run_gateway_usage(
                run_id,
                runs_dir=configurable.runs_dir,
                config={"configurable": configurable.model_dump(mode="json")},
            )
        reserved = _outstanding_usage_budget(configurable, run_id)
        response = store.get_usage_accounting(
            run_id,
            reserved_budget=reserved,
        )
    except (OSError, RuntimeError, sqlite3.Error):
        logger.warning(
            "Token accounting storage unavailable for run %s",
            run_id,
            exc_info=True,
        )
        response = _unavailable_usage_response(
            run_id,
            status=status,
            configurable=configurable,
            reason="storage_unavailable",
        )
    if response["status"] == "unknown" or (
        status in {"completed", "failed", "cancelled", "interrupted"}
        and response["status"]
        not in {"completed", "failed", "cancelled", "interrupted", "unknown"}
    ):
        # The run-level status is authoritative once terminal; a trace row
        # left "running" by an unfinished path must not leak to clients.
        response["status"] = status
    if not response.get("duration_ms"):
        response["duration_ms"] = duration_ms
    return response


def _apply_gateway_cost(
    response: dict[str, Any],
    *,
    spend_micro_usd: int,
    source: str,
    updated_at: float | None,
) -> dict[str, Any]:
    """Overlay LiteLLM's cumulative spend onto the local logical-usage projection."""
    totals = response.setdefault("totals", {})
    totals["cost"] = {
        "estimated_cost_micro_usd": spend_micro_usd,
        "cost_source": source,
        "price_table_hash": None,
    }
    budgets = totals.setdefault("budgets", {})
    cost_budget = budgets.setdefault("cost_micro_usd", {})
    cost_budget.update(settled=spend_micro_usd, estimated=0, reserved=0)
    response["cost_source"] = source
    if updated_at is not None:
        response["updated_at"] = updated_at
    return response


async def _attach_gateway_stage_breakdown(
    response: dict[str, Any],
    run_id: str,
    *,
    settings: RunKeySettings,
) -> None:
    """Overlay gateway-side per-stage spend as an authoritative comparison.

    Rows are matched by the ``run:{id}`` request tag (the gateway stores key
    hashes, so key-based filtering is unreliable) and bounded to recent
    history; every failure path just skips the overlay and the local by-stage
    accounting remains the base view.
    """
    client = LiteLLMSpendClient(settings)
    try:
        logs = await client.run_spend_logs(run_id)
    except (httpx.HTTPError, ValueError, RuntimeError):
        return
    finally:
        await client.aclose()
    if not logs:
        return
    breakdowns = response.setdefault("breakdowns", {})
    breakdowns["by_stage_gateway"] = [
        {
            "stage": bucket.key,
            "calls": bucket.calls,
            "total_tokens": bucket.total_tokens,
            "spend_micro_usd": bucket.spend_micro_usd,
        }
        for bucket in aggregate_spend_logs_by_tag_prefix(logs, "stage:")
    ]


async def _reconcile_litellm_usage(
    run_id: str,
    response: dict[str, Any],
    *,
    configurable: Configuration,
    manifest: Any,
) -> dict[str, Any]:
    if configurable.model_backend != "litellm":
        return response
    # The stage breakdown matches spend-log request tags instead of the run
    # key, so it stays available for terminal runs whose key was finalized.
    try:
        await _attach_gateway_stage_breakdown(
            response,
            run_id,
            settings=RunKeySettings.from_env(),
        )
    except LiteLLMKeyConfigurationError:
        pass
    admin: LiteLLMKeyAdminClient | None = None
    try:
        settings = RunKeySettings.from_env()
        store = RunKeySecretStore(configurable.runs_dir, settings.encryption_key)
        admin = LiteLLMKeyAdminClient(settings)
        manager = RunKeyManager(settings, store, admin)
        spend = await manager.authoritative_spend_micro_usd(run_id)
        updated_at = time.time()
        context = RunContextStore(run_id, runs_dir=configurable.runs_dir)
        context.update_reconciliation_fields(
            litellm_spend_micro_usd=spend,
            litellm_spend_updated_at=updated_at,
        )
        return _apply_gateway_cost(
            response,
            spend_micro_usd=spend,
            source="litellm_gateway",
            updated_at=updated_at,
        )
    except Exception:  # noqa: BLE001 - return explicit stale state
        snapshot = getattr(manifest, "litellm_spend_micro_usd", None)
        updated_at = getattr(manifest, "litellm_spend_updated_at", None)
        if snapshot is not None:
            return _apply_gateway_cost(
                response,
                spend_micro_usd=int(snapshot),
                source="stale_gateway_snapshot",
                updated_at=updated_at,
            )
        # No gateway snapshot exists yet; keep the local accounting labels so
        # callers see an honest local estimate instead of a bogus "stale" flag.
        return response
    finally:
        if admin is not None:
            await admin.aclose()


@app.get("/runs/{run_id}/usage")
async def get_run_usage_accounting(
    run_id: str,
    user: Principal = Depends(require_permissions(RESEARCH_RUN_READ_OWN.code)),
) -> dict[str, Any]:
    """Return content-free token accounting for one owned research run."""
    if _native_research_service is not None:
        from open_deep_research.agentscope_runtime.usage_projection import project_usage
        try:
            return await project_usage(_native_research_service.store, run_id, user.user_id,
                _unavailable_usage_response(run_id, configurable=Configuration.from_runnable_config(None)))
        except KeyError:
            pass
    record, configurable = _require_run_owner(run_id, user)
    manifest = None
    with contextlib.suppress(ValueError, JournalCorruptedError, OSError):
        manifest = RunContextStore(
            run_id,
            runs_dir=configurable.runs_dir,
        ).load_manifest()
    manifest_status = manifest.status if manifest is not None else None
    if record is None:
        status = manifest_status or "unknown"
    elif (
        manifest_status in {"completed", "failed", "cancelled", "interrupted"}
        and record.status != manifest_status
    ):
        # The durable manifest is authoritative once terminal; an in-memory
        # record can lag behind a just-finished run.
        status = manifest_status
    else:
        status = record.status
    duration_ms = (
        max(0, int((manifest.updated_at - manifest.created_at) * 1000))
        if manifest is not None and status in {
            "completed", "failed", "cancelled", "interrupted",
        }
        else None
    )
    response = await asyncio.to_thread(
        _load_run_usage_response,
        run_id,
        status=status,
        configurable=configurable,
        duration_ms=duration_ms,
    )
    return await _reconcile_litellm_usage(
        run_id,
        response,
        configurable=configurable,
        manifest=manifest,
    )


def _analytics_cursor_offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return max(0, int(base64.urlsafe_b64decode(cursor.encode()).decode()))
    except (ValueError, UnicodeDecodeError, base64.binascii.Error):
        raise HTTPException(status_code=400, detail="invalid_cursor") from None


def _sum_accounting_vectors(
    reports: list[dict[str, Any]], track: Literal["reported", "estimated"]
) -> dict[str, int]:
    keys = (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_input_tokens",
        "cache_creation_input_tokens",
        "reasoning_tokens",
    )
    return {
        key: sum(int(report["totals"][track].get(key) or 0) for report in reports)
        for key in keys
    }


def _normalize_analytics_status(status: str | None) -> str | None:
    aliases = {
        "completed": "success",
        "failed": "error",
    }
    normalized = (status or "").strip().lower()
    return aliases.get(normalized, normalized) or None


def _usage_manifest_title(
    run_id: str,
    *,
    configurable: Configuration,
    fallback: str,
) -> str:
    """Read a display title from the content store, never the usage database."""
    try:
        manifest = RunContextStore(
            run_id,
            runs_dir=configurable.runs_dir,
        ).load_manifest()
    except (ValueError, JournalCorruptedError, OSError):
        return fallback
    return str(manifest.title or fallback)


def _build_usage_analytics_response(
    *,
    range_name: Literal["7d", "30d", "retained"],
    status: str | None,
    provider: str | None,
    model: str | None,
    query: str | None,
    timezone_name: str,
    timezone_info: ZoneInfo,
    limit: int,
    offset: int,
    user_id: str,
    gateway_spend_by_run: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Build historical usage off the event loop using owner-filtered SQL.

    ``gateway_spend_by_run`` carries LiteLLM's authoritative per-run spend.
    ``None`` means the gateway was unreachable; an empty dict means reachable
    with nothing attributable. Either way the local projection stays intact.
    """
    configurable = Configuration.from_runnable_config(None)
    retention_days = float(
        configurable.run_retention_days
        if configurable.trace_retention_days is None
        else configurable.trace_retention_days
    )
    unlimited_retention = retention_days <= 0
    now = time.time()
    if range_name == "retained":
        actual_days = 0.0 if unlimited_retention else retention_days
        cutoff = None if unlimited_retention else now - actual_days * 86400
    else:
        requested_days = 7.0 if range_name == "7d" else 30.0
        actual_days = (
            requested_days
            if unlimited_retention
            else min(requested_days, retention_days)
        )
        cutoff = now - max(0.0, actual_days) * 86400

    store = SQLiteTraceStore(configurable.trace_store_path)
    owned_runs = store.list_runs_for_usage(
        user_id=user_id,
        cutoff=cutoff,
        status=_normalize_analytics_status(status),
    )
    reports: list[dict[str, Any]] = []
    run_rows: list[dict[str, Any]] = []
    selected_runs: list[tuple[dict[str, Any], str]] = []
    normalized_query = (query or "").strip().lower()
    for run in owned_runs:
        title = str(run["run_id"])
        try:
            metadata = json.loads(run.get("metadata_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        title = str(metadata.get("title") or title)
        if normalized_query:
            if normalized_query not in f"{title} {run['run_id']}".lower():
                title = _usage_manifest_title(
                    str(run["run_id"]),
                    configurable=configurable,
                    fallback=title,
                )
            if normalized_query not in f"{title} {run['run_id']}".lower():
                continue
        selected_runs.append((run, title))

    reports_by_run = store.get_usage_accounting_many(
        [str(run["run_id"]) for run, _title in selected_runs],
        provider=provider,
        model=model,
    )
    for run, title in selected_runs:
        report = reports_by_run[str(run["run_id"])]
        if (provider or model) and not report["totals"]["calls"]["attempts"]:
            continue
        reports.append(report)
        run_rows.append(
            {
                "run_id": run["run_id"],
                "title": title,
                "status": run.get("status"),
                "started_at": run.get("started_at"),
                "ended_at": run.get("ended_at"),
                "duration_ms": run.get("duration_ms"),
                "accounting_status": report["accounting_status"],
                "reported": report["totals"]["reported"],
                "estimated": report["totals"]["estimated"],
                "calls": report["totals"]["calls"],
                "cost": report["totals"]["cost"],
                "operations": report["operations"],
            }
        )

    # Gateway spend is authoritative where attributable; owner scoping comes
    # for free because only rows already filtered to this user are overlaid.
    gateway_attributed_runs = 0
    gateway_spend_total = 0
    if gateway_spend_by_run is not None:
        for row in run_rows:
            spend = gateway_spend_by_run.get(str(row["run_id"]))
            if spend is None:
                continue
            row["cost"] = {
                "estimated_cost_micro_usd": spend,
                "cost_source": "litellm_gateway",
                "price_table_hash": None,
            }
            gateway_attributed_runs += 1
            gateway_spend_total += int(spend)

    if unlimited_retention and range_name == "retained" and run_rows:
        oldest = min(float(row["started_at"] or now) for row in run_rows)
        actual_days = max(0.0, (now - oldest) / 86400)

    distributions: dict[str, dict[str, dict[str, Any]]] = {
        "provider": {},
        "model": {},
        "status": {},
    }
    daily: dict[str, dict[str, Any]] = {}
    for report, row in zip(reports, run_rows, strict=True):
        date_key = datetime.fromtimestamp(
            float(row["started_at"]),
            timezone_info,
        ).date().isoformat()
        day = daily.setdefault(
            date_key,
            {
                "date": date_key,
                "reported_tokens": 0,
                "estimated_tokens": 0,
                "run_count": 0,
                "provider_reported_responses": 0,
                "successful_responses": 0,
                "rate_429_sum": 0.0,
                "throughput_sum": 0.0,
                "cache_hit_rate_sum": 0.0,
            },
        )
        day["reported_tokens"] += int(row["reported"]["total_tokens"])
        day["estimated_tokens"] += int(row["estimated"]["total_tokens"])
        day["run_count"] += 1
        day["provider_reported_responses"] += int(
            row["calls"]["provider_reported"]
        )
        day["successful_responses"] += int(row["calls"]["successful_responses"])
        day["rate_429_sum"] += float(row["operations"]["rate_429"])
        day["throughput_sum"] += float(
            row["operations"]["output_tokens_per_second"]
        )
        day["cache_hit_rate_sum"] += float(row["operations"]["cache_hit_rate"])
        status_key = str(row["status"] or "unknown")
        status_bucket = distributions["status"].setdefault(
            status_key,
            {
                "key": status_key,
                "reported_tokens": 0,
                "estimated_tokens": 0,
                "run_count": 0,
            },
        )
        status_bucket["reported_tokens"] += int(row["reported"]["total_tokens"])
        status_bucket["estimated_tokens"] += int(row["estimated"]["total_tokens"])
        status_bucket["run_count"] += 1
        for bucket in report["breakdowns"]["by_model"]:
            full_key = str(bucket["key"])
            provider_key, _, model_key = full_key.partition(":")
            if not model_key:
                model_key = provider_key
                provider_key = "unknown"
            for dimension, key in (
                ("provider", provider_key),
                ("model", model_key),
            ):
                target = distributions[dimension].setdefault(
                    key,
                    {
                        "key": key,
                        "reported_tokens": 0,
                        "estimated_tokens": 0,
                        "call_count": 0,
                    },
                )
                target["reported_tokens"] += int(
                    bucket["reported"]["total_tokens"]
                )
                target["estimated_tokens"] += int(
                    bucket["estimated"]["total_tokens"]
                )
                target["call_count"] += int(bucket["call_count"])

    daily_rows: list[dict[str, Any]] = []
    for day in sorted(daily.values(), key=lambda item: item["date"]):
        count = max(1, int(day["run_count"]))
        successful = int(day.pop("successful_responses"))
        provider_reported = int(day.pop("provider_reported_responses"))
        day["coverage_ratio"] = (
            provider_reported / successful if successful else 0.0
        )
        day["rate_429"] = float(day.pop("rate_429_sum")) / count
        day["output_tokens_per_second"] = (
            float(day.pop("throughput_sum")) / count
        )
        day["cache_hit_rate"] = float(day.pop("cache_hit_rate_sum")) / count
        daily_rows.append(day)

    page = run_rows[offset : offset + limit]
    for item in page:
        item["title"] = _usage_manifest_title(
            str(item["run_id"]),
            configurable=configurable,
            fallback=str(item["title"]),
        )
    next_offset = offset + len(page)
    next_cursor = (
        base64.urlsafe_b64encode(str(next_offset).encode()).decode()
        if next_offset < len(run_rows)
        else None
    )
    costs = [
        row["cost"]["estimated_cost_micro_usd"]
        for row in run_rows
        if row["cost"]["estimated_cost_micro_usd"] is not None
    ]
    successful = sum(int(row["calls"]["successful_responses"]) for row in run_rows)
    provider_reported = sum(
        int(row["calls"]["provider_reported"]) for row in run_rows
    )
    return {
        "schema_version": 1,
        "range": range_name,
        "timezone": timezone_name,
        "retention_days": retention_days,
        "actual_range_days": actual_days,
        "summary": {
            "run_count": len(run_rows),
            "reported": _sum_accounting_vectors(reports, "reported"),
            "estimated": _sum_accounting_vectors(reports, "estimated"),
            "estimated_cost_micro_usd": (
                sum(int(value) for value in costs) if costs else None
            ),
            "coverage_ratio": (
                provider_reported / successful if successful else 0.0
            ),
            "gateway_spend_micro_usd": (
                gateway_spend_total if gateway_spend_by_run is not None else None
            ),
            "gateway_attributed_runs": (
                gateway_attributed_runs if gateway_spend_by_run is not None else None
            ),
            "gateway_status": (
                "ok" if gateway_spend_by_run is not None else "unavailable"
            ),
        },
        "daily": daily_rows,
        "distributions": {
            key: list(value.values()) for key, value in distributions.items()
        },
        "runs": page,
        "next_cursor": next_cursor,
    }


async def _fetch_gateway_run_spend() -> dict[str, int] | None:
    """Load authoritative per-run spend from LiteLLM; ``None`` when offline.

    Fail-open by contract: analytics must keep working with purely local
    accounting whenever the gateway or its control-plane env is unavailable.
    """
    if Configuration.from_runnable_config(None).model_backend != "litellm":
        return None
    try:
        settings = RunKeySettings.from_env()
    except LiteLLMKeyConfigurationError:
        return None
    client = LiteLLMSpendClient(settings)
    try:
        entries = await client.list_keys_with_spend()
    except (httpx.HTTPError, ValueError, RuntimeError):
        logger.warning("LiteLLM key spend list unavailable", exc_info=True)
        return None
    finally:
        await client.aclose()
    return run_spend_index(entries)


@app.get("/usage/analytics")
async def get_usage_analytics(
    range: Literal["7d", "30d", "retained"] = "30d",  # noqa: A002
    status: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    query: str | None = None,
    timezone: str = "Asia/Shanghai",
    limit: int = 50,
    cursor: str | None = None,
    user: Principal = Depends(require_permissions(RESEARCH_RUN_READ_OWN.code)),
) -> dict[str, Any]:
    """Aggregate retained token usage for the current run owner only."""
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
    try:
        timezone_info = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        raise HTTPException(status_code=422, detail="invalid timezone") from None
    offset = _analytics_cursor_offset(cursor)
    gateway_spend_by_run = await _fetch_gateway_run_spend()
    return await asyncio.to_thread(
        _build_usage_analytics_response,
        range_name=range,
        status=status,
        provider=provider,
        model=model,
        query=query,
        timezone_name=timezone,
        timezone_info=timezone_info,
        limit=limit,
        offset=offset,
        user_id=_user_identity(user),
        gateway_spend_by_run=gateway_spend_by_run,
    )


@app.get("/observability/runs")
async def list_observed_runs(
    limit: int = 100,
    user: Principal = Depends(require_permissions(RESEARCH_OBSERVABILITY_READ_OWN.code)),
) -> dict[str, Any]:
    """Return persisted observed run summaries."""
    store = _observability_store()
    return {"runs": store.list_runs(limit=limit, user_id=_user_identity(user))}


@app.get("/observability/runs/{run_id}")
async def get_observed_run(
    run_id: str,
    user: Principal = Depends(require_permissions(RESEARCH_OBSERVABILITY_READ_OWN.code)),
) -> dict[str, Any]:
    """Return one persisted observed run summary."""
    store = _observability_store()
    run = store.get_run(run_id, user_id=_user_identity(user))
    if run is None:
        raise HTTPException(status_code=404, detail="Observed run not found")
    return {"run": run}


@app.get("/observability/runs/{run_id}/spans")
async def get_observed_run_spans(
    run_id: str,
    user: Principal = Depends(require_permissions(RESEARCH_OBSERVABILITY_READ_OWN.code)),
) -> dict[str, Any]:
    """Return ordered spans for a persisted observed run."""
    store = _observability_store()
    if store.get_run(run_id, user_id=_user_identity(user)) is None:
        raise HTTPException(status_code=404, detail="Observed run not found")
    return {"run_id": run_id, "spans": store.list_spans(run_id)}


@app.get("/observability/runs/{run_id}/usage")
async def get_observed_run_usage(
    run_id: str,
    user: Principal = Depends(require_permissions(RESEARCH_OBSERVABILITY_READ_OWN.code)),
) -> dict[str, Any]:
    """Return token usage aggregate for a persisted observed run."""
    store = _observability_store()
    if store.get_run(run_id, user_id=_user_identity(user)) is None:
        raise HTTPException(status_code=404, detail="Observed run not found")
    return {"run_id": run_id, "usage": store.get_usage(run_id)}


@app.get("/observability/runs/{run_id}/metrics")
async def get_observed_run_metrics(
    run_id: str,
    user: Principal = Depends(require_permissions(RESEARCH_OBSERVABILITY_READ_OWN.code)),
) -> dict[str, Any]:
    """Return token usage, retry counts, and 429 rate for a persisted observed run."""
    store = _observability_store()
    if store.get_run(run_id, user_id=_user_identity(user)) is None:
        raise HTTPException(status_code=404, detail="Observed run not found")
    return {"run_id": run_id, "metrics": store.get_metrics(run_id)}


@app.get("/observability/ui", response_class=HTMLResponse)
async def observability_ui(
    run_id: str | None = None,
    user: Principal = Depends(require_permissions(RESEARCH_OBSERVABILITY_READ_OWN.code)),
) -> HTMLResponse:
    """Render a small server-side observability page."""
    store = _observability_store()
    identity = _user_identity(user)
    runs = store.list_runs(limit=50, user_id=identity)
    selected = run_id or (runs[0]["run_id"] if runs else None)
    selected_run = store.get_run(selected, user_id=identity) if selected else None
    if selected and selected_run is None:
        raise HTTPException(status_code=404, detail="Observed run not found")
    spans = store.list_spans(selected) if selected else []
    run_links = "".join(
        "<li><a href='/observability/ui?run_id="
        + html.escape(str(run["run_id"]))
        + "'>"
        + html.escape(str(run["run_id"]))
        + "</a> "
        + html.escape(str(run.get("status") or ""))
        + " "
        + str(run.get("total_tokens") or 0)
        + " tokens</li>"
        for run in runs
    )
    usage = store.get_usage(selected) if selected else {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    metrics = store.get_metrics(selected) if selected else {
        "retry_count": 0,
        "rate_limited_count": 0,
        "rate_429": 0.0,
        "total_llm_tool_calls": 0,
        "cache_hit_rate": 0.0,
        "tool_success_rate": 0.0,
    }
    run_title = html.escape(str(selected or "No observed runs"))
    run_status = html.escape(str((selected_run or {}).get("status") or ""))
    rows = _span_tree_rows(spans)
    rate_pct = f"{(metrics.get('rate_429') or 0) * 100:.1f}%"
    cache_hit_pct = f"{(metrics.get('cache_hit_rate') or 0) * 100:.1f}%"
    cache_input_pct = f"{(metrics.get('cache_input_ratio') or 0) * 100:.1f}%"
    tool_success_pct = f"{(metrics.get('tool_success_rate') or 0) * 100:.1f}%"
    body = f"""
    <!doctype html>
    <html>
    <head>
      <meta charset='utf-8'>
      <title>Open Deep Research Observability</title>
      <style>
        body {{ font-family: system-ui, sans-serif; margin: 24px; color: #1f2937; }}
        main {{ display: grid; grid-template-columns: 320px 1fr; gap: 24px; }}
        a {{ color: #0f766e; text-decoration: none; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
        th, td {{ border-bottom: 1px solid #e5e7eb; padding: 8px; text-align: left; vertical-align: top; }}
        th {{ background: #f8fafc; }}
        .metric {{ display: inline-block; margin-right: 16px; padding: 8px 10px; background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 6px; }}
        .error td {{ background: #fef2f2; }}
        .ok td {{ background: #ffffff; }}
        aside {{ border-right: 1px solid #e5e7eb; padding-right: 16px; }}
        ul {{ padding-left: 18px; }}
      </style>
    </head>
    <body>
      <h1>Open Deep Research Observability</h1>
      <main>
        <aside>
          <h2>Runs</h2>
          <ul>{run_links}</ul>
        </aside>
        <section>
          <h2>{run_title}</h2>
          <div class='metric'>status: {run_status}</div>
          <div class='metric'>input: {usage['input_tokens']}</div>
          <div class='metric'>output: {usage['output_tokens']}</div>
          <div class='metric'>total: {usage['total_tokens']}</div>
          <div class='metric'>cached input: {usage.get('cached_input_tokens', 0)}</div>
          <div class='metric'>reasoning: {usage.get('reasoning_tokens', 0)}</div>
          <div class='metric'>estimated cost: ${usage.get('estimated_cost_usd', 0):.6f}</div>
          <div class='metric'>attempts: {metrics.get('attempt_count', 0)}</div>
          <div class='metric'>retries: {metrics.get('retry_count', 0)}</div>
          <div class='metric'>429 call rate: {rate_pct} ({metrics.get('rate_limited_count', 0)}/{metrics.get('total_llm_tool_calls', 0)} calls; {metrics.get('rate_limit_events', 0)} events)</div>
          <div class='metric'>cache hit: {cache_hit_pct} ({metrics.get('cache_hit_count', 0)}/{metrics.get('cache_eligible_count', 0)} calls)</div>
          <div class='metric'>cached input ratio: {cache_input_pct}</div>
          <div class='metric'>output throughput: {metrics.get('llm_output_tokens_per_second', 0):.1f} token/s</div>
          <div class='metric'>tool success: {tool_success_pct} ({metrics.get('tool_success_count', 0)}/{metrics.get('tool_call_count', 0)})</div>
          <div class='metric'>empty tool results: {metrics.get('empty_tool_result_count', 0)}</div>
          <div class='metric'>zero-source searches: {metrics.get('zero_source_search_count', 0)}</div>
          <table>
            <thead><tr><th>Span</th><th>Kind</th><th>Status</th><th>Duration ms</th><th>Tokens</th><th>Retries</th><th>Error type</th><th>Error</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </section>
      </main>
    </body>
    </html>
    """
    return HTMLResponse(body)


@app.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    user: Principal = Depends(require_permissions(RESEARCH_RUN_CONTROL_OWN.code)),
) -> dict[str, str]:
    """Cancel a background run."""
    record, configurable = _require_run_owner(run_id, user)
    terminal_statuses = {"completed", "failed", "cancelled"}
    if record is not None:
        if record.status in terminal_statuses:
            return {"run_id": run_id, "status": record.status}
        record.engine.interrupt()
        record.status = "cancelling"
    else:
        manifest = RunContextStore(
            run_id,
            runs_dir=configurable.runs_dir,
        ).load_manifest()
        if manifest.status in terminal_statuses:
            return {"run_id": run_id, "status": manifest.status}
        await RunControlStore(run_id, runs_dir=configurable.runs_dir).enqueue(
            "cancel",
            {},
            command_id=f"cancel-{run_id}",
        )
    logger.info(
        "run cancellation requested",
        extra={"actor": _user_identity(user), "action": "run.cancel", "run_id": run_id},
    )
    return {"run_id": run_id, "status": "cancelling"}


async def _cancel_before_forced_purge(
    run_id: str,
    record: RunRecord | None,
    configurable: Configuration,
) -> None:
    """Use the existing interrupt/control mechanisms before destructive purge."""
    if record is not None:
        record.engine.interrupt()
        if record.task is not None and not record.task.done():
            record.task.cancel()
            await asyncio.gather(record.task, return_exceptions=True)
        record.status = "cancelled"
        record.finished_at = time.time()
        store = getattr(record.engine, "context_store", None)
        if store is not None and store.manifest_path.exists():
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    store._update_manifest,  # noqa: SLF001
                    status="cancelled",
                )
        return
    await RunControlStore(run_id, runs_dir=configurable.runs_dir).enqueue(
        "cancel",
        {},
        command_id=f"cancel-before-purge-{run_id}",
    )


@app.delete("/runs/{run_id}")
async def delete_run(
    run_id: str,
    force: bool = False,
    dry_run: bool = False,
    user: Principal = Depends(require_permissions(RESEARCH_RUN_CONTROL_OWN.code)),
) -> dict[str, Any]:
    """Permanently delete an owned run and all durable observability rows."""
    is_admin = "admin" in user.roles
    if is_admin:
        record = _runs.get(run_id)
        configurable = Configuration.from_runnable_config(
            getattr(record.engine, "config", None) if record is not None else None
        )
        target = _run_directory(configurable, run_id)
        trace_exists = await asyncio.to_thread(
            SQLiteTraceStore(configurable.trace_store_path).get_run,
            run_id,
        )
        if record is None and not target.exists() and trace_exists is None:
            raise HTTPException(status_code=404, detail="Run not found")
    else:
        record, configurable = _require_run_owner(run_id, user)
        target = _run_directory(configurable, run_id)

    manifest = None
    if target.exists():
        with contextlib.suppress(JournalCorruptedError, OSError, ValueError):
            manifest = await asyncio.to_thread(
                RunContextStore(run_id, runs_dir=configurable.runs_dir).load_manifest
            )
    status = record.status if record is not None else getattr(manifest, "status", None)
    if status not in _TERMINAL_RUN_STATUSES and not force:
        raise HTTPException(status_code=409, detail="run_is_active")
    if dry_run:
        return {
            "run_id": run_id,
            "status": "would_delete",
            "run_status": status,
            "force": force,
            "directory": str(target),
            "trace_store": configurable.trace_store_path,
        }
    if status not in _TERMINAL_RUN_STATUSES:
        await _cancel_before_forced_purge(run_id, record, configurable)
    return await _purge_run_artifacts(
        run_id,
        configurable,
        reason="manual",
        actor=_user_identity(user),
        require_terminal=not force,
    )
