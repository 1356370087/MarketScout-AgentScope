"""FastAPI composition for AgentScope research and read-only historical runs."""

import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from open_deep_research.api.access import RunAccess
from open_deep_research.api.activity_routes import ActivityRoutes
from open_deep_research.api.admission import ApiAdmission
from open_deep_research.api.configuration_routes import router as configuration_router
from open_deep_research.api.lifecycle import ApplicationLifecycle
from open_deep_research.api.middleware import request_body_limit_middleware, request_id_middleware
from open_deep_research.api.observability import router as observability_router
from open_deep_research.api.operations import OperationalRoutes
from open_deep_research.api.run_usage import RunUsageRoutes
from open_deep_research.api.security_routes import SecurityRoutes
from open_deep_research.api.stream_host import ApplicationStreams
from open_deep_research.api.usage import router as usage_router
from open_deep_research.agentscope_runtime.native_host import mount_native_research
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
_native_research_service = None
_admission = ApiAdmission()


def _set_native_research_service(service):
    global _native_research_service
    _native_research_service = service


class _ResearchService:
    """Expose stable route schemas before lifespan binds the owned service."""

    def __getattr__(self, name):
        if _native_research_service is None:
            raise HTTPException(503, "runtime_unavailable")
        return getattr(_native_research_service, name)


_access = RunAccess(lambda: _native_research_service)
_lifecycle = ApplicationLifecycle(
    get_native_service=lambda: _native_research_service,
    set_native_service=_set_native_research_service,
    native_key_cleanup=_access.key_cleanup,
    admission=lambda: _admission,
)
app = FastAPI(title="InsightForge", version="0.1.0", lifespan=_lifecycle.lifespan)
app.middleware("http")(request_id_middleware)
app.middleware("http")(request_body_limit_middleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[item.strip() for item in os.environ.get(
        "FRONTEND_ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
    ).split(",") if item.strip()],
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "Last-Event-ID"],
)
mount_rbac(app)
register_ownership_checker("run", _access.run_owner)
register_ownership_checker("task", _access.task_owner)
for router in (documents_router, knowledge_router, knowledge_search_router, batch_router,
               fact_wiki_router, health_router, workspace_router, configuration_router,
               usage_router, observability_router):
    app.include_router(router)

app.include_router(build_internal_sandbox_router(
    lambda _run_id: None,
    native_ledger=_access.sandbox_ledger,
    native_root_key=lambda: Configuration.from_runnable_config(None).sandbox_root_signing_key,
))
_operational_routes = OperationalRoutes(
    native_service=lambda: _native_research_service,
    shutting_down=_lifecycle.shutting_down,
    metrics_path=Configuration.from_runnable_config(None).prometheus_metrics_path,
)
app.include_router(_operational_routes.router)
_security_routes = SecurityRoutes(sandbox_store_context=_access.sandbox_context, run_owner_checker=_access.run_owner)
app.include_router(_security_routes.router)
_streams = ApplicationStreams(_lifecycle.sse_shutdown)
_activity_routes = ActivityRoutes(
    native_service=lambda: _native_research_service,
    lookup_record=lambda _: None,
    require_run_owner=_access.require_history_owner,
    require_record_owner=lambda *_: None,
    reserve_sse_connection=_admission._reserve_sse_connection,
    limited_sse=_admission._limited_sse,
    task_activity_iterator=_streams._task_activity_iterator,
    public_event_iterator=_streams._public_event_iterator,
)
# NativeRuns owns run events, including archive replay; this router contributes
# only the separate task-activity cursor and transcript projection.
_activity_routes.router.routes[:] = [route for route in _activity_routes.router.routes
                                    if getattr(route, "path", "") != "/runs/{run_id}/events"]
app.include_router(_activity_routes.router)
_usage_routes = RunUsageRoutes(native_service=lambda: _native_research_service,
                              require_run_owner=_access.require_history_owner)
app.include_router(_usage_routes.router)
mount_native_research(app, _ResearchService())
