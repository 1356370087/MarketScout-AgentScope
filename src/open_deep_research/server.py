"""FastAPI service entrypoint for the LangGraph-free runtime."""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from open_deep_research.agents.query_engine import QueryEngine
from open_deep_research.api.projections import _stable_output, _stable_report_review  # noqa: F401
from open_deep_research.api.stream_host import ApplicationStreams, _reauthorize_stream  # noqa: F401
from open_deep_research.api.lifecycle import ApplicationLifecycle
from open_deep_research.api_host.run_access import (
    RunAccess, _config_from_request, _validate_run_sources,
    _find_idempotent_run, _require_record_owner, _augment_run_projection,
)
from open_deep_research.api.middleware import request_id_middleware, request_body_limit_middleware
from open_deep_research.api_host.operations import (
    OperationalRoutes, healthz, _refresh_operational_metrics,
    _probe_runs_directory, _search_readiness,  # noqa: F401 - compatibility exports
)
from open_deep_research.api_host.run_admission import RunAdmission, _user_identity
from open_deep_research.api_host.run_recovery import (
    _runs_root, _load_manifests, _run_recovery_sweep,
    _fenced_recovery_config, _renew_sweep_lease,  # noqa: F401 - compatibility exports
)
from open_deep_research.api_host.run_execution import (
    RunExecution,
    _release_gateway_run,  # noqa: F401 - compatibility export
    _run_control_listener,  # noqa: F401 - compatibility export
)
from open_deep_research.api_host.run_registry import RunRegistry, RunRecord, _new_run_record, _interrupt_inflight_record  # noqa: F401
from open_deep_research.api_host.run_start import RunStartRoutes, _request_query_preview, _run_title  # noqa: F401
from open_deep_research.api_host.run_retention import (
    RunRetention,
    _TERMINAL_RUN_STATUSES,
    _cancel_before_forced_purge,  # noqa: F401 - compatibility export
    _lifecycle_metrics,  # noqa: F401 - compatibility export
    _load_manifests_from_root,  # noqa: F401 - compatibility export
    _record_lifecycle_error,  # noqa: F401 - compatibility export
    _run_directory,  # noqa: F401 - compatibility export
    _run_finished_at,  # noqa: F401 - compatibility export
    _runs_dir_size_bytes,  # noqa: F401 - compatibility export
)
from open_deep_research.api_host.run_interactions import RunInteractionRoutes
from open_deep_research.api.run_reads import RunReadRoutes, _encode_cursor, _decode_cursor  # noqa: F401
from open_deep_research.api.run_usage import (
    RunUsageRoutes,
    _unavailable_usage_response,  # noqa: F401 - compatibility exports
    _outstanding_usage_budget,  # noqa: F401
    _load_run_usage_response,  # noqa: F401
    _apply_gateway_cost,  # noqa: F401
    _attach_gateway_stage_breakdown,  # noqa: F401
    _reconcile_litellm_usage,  # noqa: F401
)
from open_deep_research.api.configuration_routes import router as configuration_router
from open_deep_research.api.activity_routes import ActivityRoutes
from open_deep_research.api_host.security_routes import SecurityRoutes
from open_deep_research.api.publication_routes import (
    PublicationRoutes,
    _run_publications,
)
from open_deep_research.api.observability import (
    router as observability_router,
)
from open_deep_research.api.usage import (
    _build_usage_analytics_response,  # noqa: F401 - compatibility for existing callers
    get_usage_analytics,  # noqa: F401
    router as usage_router,
)
from open_deep_research.api.contracts import (
    HumanActionRequest,
    HumanFeedbackRequest,
    ResumeRunRequest,
    RunRequest,
    TeamMessageRequest,
)
from open_deep_research.api.streams import _sse_headers
from open_deep_research.configuration import Configuration
from open_deep_research.documents.router import router as documents_router
from open_deep_research.knowledge.batch_router import router as batch_router
from open_deep_research.knowledge.fact_wiki_router import router as fact_wiki_router
from open_deep_research.knowledge.health_router import router as health_router
from open_deep_research.knowledge.router import router as knowledge_router
from open_deep_research.knowledge.search_router import router as knowledge_search_router
from open_deep_research.knowledge.workspace_router import router as workspace_router
from open_deep_research.logging_config import configure_logging
from open_deep_research.sandbox.internal_api import build_internal_sandbox_router
from security.rbac import mount_rbac, register_ownership_checker

load_dotenv()
configure_logging()


def _set_native_research_service(service):
    global _native_research_service
    _native_research_service = service


_lifecycle = ApplicationLifecycle(
    get_native_service=lambda: _native_research_service,
    set_native_service=_set_native_research_service,
    recovery_sweep=lambda config: _run_recovery_sweep(config),
    retention_loop=lambda config: _retention_sweep_loop(config),
    native_key_cleanup=lambda *args: _native_key_cleanup(*args),
    drain_runs=lambda timeout: _drain_inflight_runs(timeout),
    eviction_tasks=lambda: _run_eviction_tasks,
)
_lifespan = _lifecycle.lifespan


app = FastAPI(title="Open Deep Research", version="0.1.0", lifespan=_lifespan)


app.middleware("http")(request_id_middleware)
app.middleware("http")(request_body_limit_middleware)

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


_run_access = RunAccess(
    lookup_record=lambda run_id: _runs.get(run_id),
    native_service=lambda: _native_research_service,
)
_rbac_run_owner_checker = _run_access._rbac_run_owner_checker
_rbac_task_owner_checker = _run_access._rbac_task_owner_checker
_require_run_owner = _run_access._require_run_owner
_resolve_internal_sandbox_run = _run_access._resolve_internal_sandbox_run
_native_key_cleanup = _run_access._native_key_cleanup
_native_sandbox_ledger = _run_access._native_sandbox_ledger
_sandbox_store_context = _run_access._sandbox_store_context

register_ownership_checker("run", _rbac_run_owner_checker)
register_ownership_checker("task", _rbac_task_owner_checker)

logger = logging.getLogger(__name__)
_run_registry = RunRegistry()
_runs = _run_registry.runs
_run_eviction_tasks = _run_registry.eviction_tasks
_drain_inflight_runs = _run_registry._drain_inflight_runs
_enforce_run_memory_limit = _run_registry._enforce_run_memory_limit
_evict_run_after_delay = _run_registry._evict_run_after_delay
_evict_run_record = _run_registry._evict_run_record
_remember_run = _run_registry._remember_run
_schedule_run_eviction = _run_registry._schedule_run_eviction

_run_admission = RunAdmission(lambda: _runs)
_api_rate_limiter = _run_admission.rate_limiter
_sse_connection_limiter = _run_admission.connection_limiter
_active_runs_for_user = _run_admission._active_runs_for_user
_enforce_run_create_limits = _run_admission._enforce_run_create_limits
_reserve_sse_connection = _run_admission._reserve_sse_connection
_limited_sse = _run_admission._limited_sse
_shutting_down = _lifecycle.shutting_down
_sse_shutdown = _lifecycle.sse_shutdown
_metrics_path = Configuration.from_runnable_config(None).prometheus_metrics_path


_native_research_service = None


app.include_router(
    build_internal_sandbox_router(
        _resolve_internal_sandbox_run, native_ledger=_native_sandbox_ledger,
        native_root_key=lambda: Configuration.from_runnable_config(None).sandbox_root_signing_key,
    )
)


_operational_routes = OperationalRoutes(
    native_service=lambda: _native_research_service,
    shutting_down=_shutting_down,
    metrics_path=_metrics_path,
)
app.include_router(_operational_routes.router)
_readiness_report = _operational_routes._readiness_report
readyz = _operational_routes.readyz
prometheus_metrics = _operational_routes.prometheus_metrics


_stream_host = ApplicationStreams(_sse_shutdown)
_stream_options = _stream_host._stream_options
_public_event_iterator = _stream_host._public_event_iterator
_publication_event_iterator = _stream_host._publication_event_iterator
_task_activity_iterator = _stream_host._task_activity_iterator


_run_execution = RunExecution(lambda record, config: _schedule_run_eviction(record, config))
_run_background = _run_execution._run_background
_run_resumed_background = _run_execution._run_resumed_background


app.include_router(usage_router)

app.include_router(observability_router)

_publication_routes = PublicationRoutes(
    require_run_owner=lambda *args: _require_run_owner(*args),
    reserve_sse_connection=lambda *args: _reserve_sse_connection(*args),
    limited_sse=lambda *args: _limited_sse(*args),
    publication_event_iterator=lambda *args, **kwargs: _publication_event_iterator(*args, **kwargs),
)
app.include_router(_publication_routes.router)
# Compatibility for callers that invoke the former endpoint directly.
retry_publication = _publication_routes.retry_publication

_security_routes = SecurityRoutes(
    sandbox_store_context=lambda *args, **kwargs: _sandbox_store_context(*args, **kwargs),
    run_owner_checker=lambda *args: _rbac_run_owner_checker(*args),
)
app.include_router(_security_routes.router)

_activity_routes = ActivityRoutes(
    native_service=lambda: _native_research_service,
    lookup_record=lambda run_id: _runs.get(run_id),
    require_run_owner=lambda *args, **kwargs: _require_run_owner(*args, **kwargs),
    require_record_owner=lambda *args, **kwargs: _require_record_owner(*args, **kwargs),
    reserve_sse_connection=lambda *args, **kwargs: _reserve_sse_connection(*args, **kwargs),
    limited_sse=lambda *args, **kwargs: _limited_sse(*args, **kwargs),
    task_activity_iterator=lambda *args, **kwargs: _task_activity_iterator(*args, **kwargs),
    public_event_iterator=lambda *args, **kwargs: _public_event_iterator(*args, **kwargs),
)
app.include_router(_activity_routes.router)

app.include_router(configuration_router)

_run_usage_routes = RunUsageRoutes(
    native_service=lambda: _native_research_service,
    require_run_owner=lambda *args: _require_run_owner(*args),
)
app.include_router(_run_usage_routes.router)
get_run_usage_accounting = _run_usage_routes.get_run_usage_accounting

_run_read_routes = RunReadRoutes(
    load_manifests=lambda: _load_manifests(),
    lookup_record=lambda run_id: _runs.get(run_id),
    require_record_owner=lambda *args: _require_record_owner(*args),
    augment_projection=lambda *args: _augment_run_projection(*args),
)
app.include_router(_run_read_routes.router)
list_runs = _run_read_routes.list_runs
get_run = _run_read_routes.get_run

_run_interaction_routes = RunInteractionRoutes(
    require_run_owner=lambda *args: _require_run_owner(*args),
)
app.include_router(_run_interaction_routes.router)
submit_human_action = _run_interaction_routes.submit_human_action
submit_feedback = _run_interaction_routes.submit_feedback
get_research_team = _run_interaction_routes.get_research_team
send_team_message = _run_interaction_routes.send_team_message
cancel_run = _run_interaction_routes.cancel_run

_run_retention = RunRetention(
    runs=lambda: _runs,
    eviction_tasks=lambda: _run_eviction_tasks,
    require_run_owner=lambda *args: _require_run_owner(*args),
)
app.include_router(_run_retention.router)
_purge_run_artifacts = _run_retention._purge_run_artifacts
_retention_sweep_loop = _run_retention._retention_sweep_loop
_run_retention_sweep = _run_retention._run_retention_sweep
delete_run = _run_retention.delete_run

_run_start_routes = RunStartRoutes(
    engine_factory=lambda config: QueryEngine(config),
    load_engine=lambda *args, **kwargs: QueryEngine.load(*args, **kwargs),
    lookup_record=lambda run_id: _runs.get(run_id),
    release_sse=lambda token: _sse_connection_limiter.release(token),
    validate_sources=lambda *args, **kwargs: _validate_run_sources(*args, **kwargs),
    config_from_request=lambda *args, **kwargs: _config_from_request(*args, **kwargs),
    enforce_limits=lambda *args, **kwargs: _enforce_run_create_limits(*args, **kwargs),
    reserve_sse=lambda *args, **kwargs: _reserve_sse_connection(*args, **kwargs),
    new_record=lambda *args, **kwargs: _new_run_record(*args, **kwargs),
    run_background=lambda *args, **kwargs: _run_background(*args, **kwargs),
    run_resumed=lambda *args, **kwargs: _run_resumed_background(*args, **kwargs),
    remember_run=lambda *args, **kwargs: _remember_run(*args, **kwargs),
    limited_sse=lambda *args, **kwargs: _limited_sse(*args, **kwargs),
    public_events=lambda *args, **kwargs: _public_event_iterator(*args, **kwargs),
    find_idempotent=lambda *args, **kwargs: _find_idempotent_run(*args, **kwargs),
    require_owner=lambda *args, **kwargs: _require_record_owner(*args, **kwargs),
)
app.include_router(_run_start_routes.router)
stream_run = _run_start_routes.stream_run
create_run = _run_start_routes.create_run
resume_run = _run_start_routes.resume_run
