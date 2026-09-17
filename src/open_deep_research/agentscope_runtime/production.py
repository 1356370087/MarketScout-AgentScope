"""Resource-owning NativeRuns factory with live identity and leased team binding."""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from open_deep_research.agentscope_runtime.gateway_ledger import SQLGatewayLedger
from open_deep_research.agentscope_runtime.research import build_research_pipeline
from open_deep_research.configuration import Configuration
from open_deep_research.tools.base import ToolExecutionZone
from security.rbac.dependencies import apply_principal_to_config
from security.rbac.permissions import RESEARCH_RUN_CREATE


async def authorize_run_owner(owner, application):
    """Reload the original session and current permissions without persisting JWTs."""
    from security.rbac.database import session_scope
    from security.rbac.dependencies import reauthorize_session
    from security.rbac.principal import Principal, synthetic_dev_principal
    from security.rbac.settings import local_dev_bypass_enabled

    if local_dev_bypass_enabled():
        principal = synthetic_dev_principal()
        return principal if principal.user_id == owner else None
    identity = application.get("identity", {})
    if not identity.get("session_id"):
        return None
    principal = Principal(
        user_id=owner,
        email="",
        status="active",
        session_id=identity["session_id"],
        roles=frozenset(),
        permissions=frozenset(),
        authz_version=identity["authz_version"],
    )
    async with session_scope() as db:
        return await reauthorize_session(db, principal)


@dataclass
class RunResources:
    """Host-authorized ports; their enclosing context owns credentials and clients."""

    models: object
    tools_for: object
    model_for: object = None
    dispatcher: object = None
    gateway_accounting: bool = False
    team_launcher: object = None
    local_zones: frozenset = frozenset({ToolExecutionZone.HOST_CONTROL})


class ProductionRunFactory:
    """Compose persisted runs without importing QueryEngine or trusting HTTP identity.

    authorize(owner, application) reloads IAM state. open_resources(run_config,
    config, recovery) is the deployment credential/sandbox context. No credential
    is restored from checkpoints and no sandbox failure falls back to host models.
    """

    def __init__(self, runtime, authorize, open_resources, *, runs_dir):
        self.runtime, self.authorize = runtime, authorize
        self.open_resources = open_resources
        self.runs_dir = Path(runs_dir)
        self.active = {}

    async def gateway_ledger(self, run_id):
        item = self.active.get(run_id)
        return item["ledger"] if item else None

    @asynccontextmanager
    async def __call__(self, snapshot, run_config, recovery):
        principal = await self.authorize(recovery.lease.user_id, snapshot.application)
        if (
            principal is None
            or principal.user_id != recovery.lease.user_id
            or not principal.is_active
            or not principal.has(RESEARCH_RUN_CREATE.code)
        ):
            raise PermissionError("run_owner_no_longer_authorized")
        config = apply_principal_to_config(
            run_config.compatibility_projection(), principal
        )
        # 交互开关不在冻结契约内；恢复时从持久化的请求配置取回。
        config["configurable"] = {
            **snapshot.application.get("request_configurable", {}),
            **config["configurable"],
        }
        config["metadata"].update(
            run_id=recovery.lease.run_id,
            deployment_surface="http",
            user_id=principal.user_id,
            run_fence_token=recovery.lease.fence,
            source_selection=snapshot.application.get("source_selection", {}),
            publication_theme=snapshot.application.get("publication_theme", {}),
        )
        selected = snapshot.application.get("selected_source_snapshots", [])
        if selected:
            from open_deep_research.documents.repository import bind_run_sources
            await bind_run_sources(recovery.lease.run_id, principal.user_id, selected)
        from open_deep_research.agentscope_runtime.native_security import NativeEventPublisher
        config["_event_publisher"] = NativeEventPublisher(recovery.store, recovery.lease)
        cfg = Configuration.from_runnable_config(config)
        await recovery.store.register_task(recovery.lease, "supervisor")
        async with self.open_resources(run_config, config, recovery) as ports:
            if cfg.sandbox_enabled and (
                ports.model_for is None
                or ports.dispatcher is None
                or not ports.gateway_accounting
            ):
                raise ValueError(
                    "sandbox requires gateway models, dispatcher and SQL accounting"
                )
            recovery.model_accounting = (
                "gateway" if ports.gateway_accounting else "local"
            )
            if cfg.enable_async_research and self.runtime is None:
                raise ValueError(
                    "native team host requires ASRuntime; deploy the runtime "
                    "or disable enable_async_research"
                )
            team = (
                await self.runtime.bind_research_team(recovery)
                if cfg.enable_async_research
                else None
            )
            pipeline = build_research_pipeline(
                run_id=recovery.lease.run_id,
                run_config=run_config,
                model_factory=ports.models,
                config_provider=lambda: config,
                tools_for=ports.tools_for,
                model_for=ports.model_for,
                dispatcher=ports.dispatcher,
                local_zones=ports.local_zones,
                checkpoint_path=self.runs_dir
                / recovery.lease.run_id
                / "native-checkpoint.json",
                recovery=recovery,
                team=team,
                team_artifact_dir=self.runs_dir,
                external_team_workers=ports.team_launcher is not None,
                team_launcher=ports.team_launcher,
            )
            self.active[recovery.lease.run_id] = {
                "team": team,
                "ledger": SQLGatewayLedger(
                    recovery, run_config.get("model_catalog_snapshot")
                )
                if ports.gateway_accounting
                else None,
            }
            ledger = self.active[recovery.lease.run_id]["ledger"]
            if ledger is not None:
                ledger.config = config
            try:
                yield pipeline
            finally:
                self.active.pop(recovery.lease.run_id, None)
                if pipeline.team_workers:
                    await pipeline.team_workers.aclose()
