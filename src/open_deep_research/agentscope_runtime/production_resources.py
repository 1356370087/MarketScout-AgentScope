"""Trusted production resources for native Web runs and team executors.

Provider secrets belong to the API/Gateway vault. Team executors are trusted
control-plane workers; they must never execute sandbox-local shell/file tools.
"""

import asyncio
import time
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import replace

from pydantic import SecretStr

from open_deep_research.agentscope_runtime.gateway import SandboxBinding
from open_deep_research.agentscope_runtime.models import (
    ROLES,
    CredentialBinding,
    ModelFactory,
)
from open_deep_research.agentscope_runtime.production import RunResources
from open_deep_research.agentscope_runtime.sandbox_policy import CapabilityTokenIssuer
from open_deep_research.configuration import Configuration
from open_deep_research.models.catalog import (
    LiteLLMModelCatalogClient,
    ModelCatalogEntry,
    freeze_catalog_snapshot,
    validate_model_catalog,
)
from open_deep_research.models.credentials import (
    LiteLLMKeyAdminClient,
    LiteLLMTeamAdminClient,
    RunKeyManager,
    RunKeySecretStore,
    RunKeySettings,
    resolve_role_team_role,
    team_alias_for_role,
    team_budget_policies_from_env,
)
from open_deep_research.sandbox.gateway_catalog import load_gateway_catalog_tools
from open_deep_research.sandbox.gateway_client import (
    SandboxGatewayControlClient,
    split_gateway_registration,
)
from open_deep_research.sandbox.gateway_tool import GatewayToolProxy
from open_deep_research.sandbox.schema import policy_digest, resolve_profile
from open_deep_research.tools.base import ToolExecutionZone


def allowed_models(config):
    values = {
        getattr(config, field) or getattr(config, fallback)
        for field, fallback, _ in ROLES.values()
    }
    values.update(model for chain in config.model_fallbacks.values() for model in chain)
    values.add(config.report_review_model or config.quality_evaluation_model)
    values.add("if-fallback-v1")
    return sorted(value for value in values if value)


async def prepare_production_config(request, principal):
    """Freeze authoritative model limits/prices before NativeRuns creates its SQL row."""
    from security.rbac.dependencies import apply_principal_to_config

    config = apply_principal_to_config(
        {"configurable": dict(request.configurable)}, principal
    )
    config.setdefault("metadata", {})["deployment_surface"] = "http"
    cfg = Configuration.from_runnable_config(config)
    if cfg.model_backend != "litellm":
        raise ValueError("production Web resources require LiteLLM")
    if request.source_selection.documents_enabled:
        from fastapi import HTTPException

        from open_deep_research.documents.database import document_schema_available
        from open_deep_research.documents.repository import (
            DocumentConflictError,
            validate_selection,
        )

        if not document_schema_available():
            raise HTTPException(503, "document_research_unavailable")
        try:
            documents = await validate_selection(
                principal.user_id, request.source_selection
            )
        except KeyError as exc:
            raise HTTPException(404, "document_not_found") from exc
        except DocumentConflictError as exc:
            raise HTTPException(409, "document_not_ready") from exc
        config.setdefault("metadata", {})["selected_source_snapshots"] = [
            {
                key: str(row[key])
                for key in ("id", "filename", "sha256", "current_generation_id")
            }
            for row in documents
        ]
    settings = RunKeySettings.from_env()
    # Freeze the proxy's default cap too, so pause/resume cannot reset its budget.
    config["configurable"]["max_run_cost_micro_usd"] = settings.resolve_budget(
        cfg.max_run_cost_micro_usd
    )
    client = LiteLLMModelCatalogClient(
        base_url=settings.base_url, api_key=settings.master_key
    )
    try:
        catalog = await client.load()
    finally:
        await client.aclose()
    names = allowed_models(cfg)
    validate_model_catalog(catalog, names, budget_enabled=True)
    config["configurable"]["model_catalog_snapshot"] = freeze_catalog_snapshot(
        catalog, names
    )
    return config


def production_resources(runs_dir, *, launcher_factory=None, worker_task_id=None, worker_member_id=None):
    """Own registration on the API only; child workers attach to its live vault."""

    @asynccontextmanager
    async def open_resources(run_config, config, recovery):
        cfg = Configuration.from_runnable_config(config)
        if cfg.model_backend != "litellm":
            raise ValueError("production Web resources require LiteLLM")
        catalog = {
            name: ModelCatalogEntry.model_validate(value)
            for name, value in run_config.get("model_catalog_snapshot").items()
        }
        names = allowed_models(cfg)
        validate_model_catalog(catalog, names, budget_enabled=True)
        run_id, fence = recovery.lease.run_id, recovery.lease.fence
        issuer = CapabilityTokenIssuer.from_root_key(cfg.sandbox_root_signing_key or "")
        bundle, profile_id, profile = resolve_profile(cfg)
        tokens = {}

        def token_for(task_id):
            if task_id not in tokens or tokens[task_id][1] <= time.time() + 30:
                token, _ = issuer.issue(
                    run_id=run_id,
                    task_id=task_id,
                    fence_token=fence,
                    profile_id=profile_id,
                    policy_digest=policy_digest(bundle),
                    ttl_seconds=float(profile.resources.timeout_seconds + 60),
                )
                tokens[task_id] = (
                    SecretStr(token),
                    time.time() + profile.resources.timeout_seconds + 60,
                )
            return tokens[task_id][0]

        async with AsyncExitStack() as stack:
            if worker_task_id is None and worker_member_id is None:
                settings = RunKeySettings.from_env()
                manager = RunKeyManager(
                    settings,
                    RunKeySecretStore(str(runs_dir), settings.encryption_key),
                    LiteLLMKeyAdminClient(settings),
                )
                stack.push_async_callback(manager.aclose)
                policies = team_budget_policies_from_env()
                user = config.get("configurable", {}).get("langgraph_auth_user") or {}
                role = resolve_role_team_role(
                    list(user.get("roles") or []),
                    user_id=recovery.lease.user_id,
                    policies=policies,
                )
                team_id = None
                if role:
                    team_admin = LiteLLMTeamAdminClient(settings)
                    try:
                        team_id = await team_admin.ensure_team(
                            team_alias_for_role(role), policies[role]
                        )
                    finally:
                        await team_admin.aclose()
                # Serialize key rotation with the leader fence: an old owner must
                # never replace/block the key acquired by its successor.
                async with recovery.store.transaction(recovery.lease) as (_, budget):
                    # Read under the same fence/row lock as key creation: a late
                    # receipt must not change the balance between read and mint.
                    limit = budget["limits"].get("cost_micro_usd")
                    remaining = (
                        None
                        if limit is None
                        else limit
                        - budget["used"].get("cost_micro_usd", 0)
                        - budget["reserved"].get("cost_micro_usd", 0)
                    )
                    if remaining is not None and remaining <= 0:
                        raise ValueError("run_cost_budget_exhausted")
                    lease = await manager.ensure(
                        run_id=run_id,
                        requested_budget_micro_usd=remaining,
                        allowed_models=names,
                        team_id=team_id,
                    )

                async def block_key():
                    from open_deep_research.agentscope_runtime.recovery_store import (
                        FenceLost,
                    )

                    try:
                        async with recovery.store.transaction(recovery.lease):
                            if not await manager.finalize(run_id):
                                raise RuntimeError("native_run_key_cleanup_pending")
                    except FenceLost:
                        # Only block our own secret after losing the fence; do not
                        # read/delete the successor's encrypted run-key file.
                        await manager.admin.block(lease.key)

                stack.push_async_callback(block_key)
                frozen, credentials = split_gateway_registration(config)
                credentials["LITELLM_RUN_KEY"] = lease.key
                control = SandboxGatewayControlClient(cfg)
                # Register cleanup before sending: a timeout may follow successful registration.
                stack.push_async_callback(
                    control.unregister_run, run_id=run_id, fence_token=fence
                )
                await control.register_run(
                    run_id=run_id,
                    fence_token=fence,
                    frozen_config=frozen,
                    api_keys=credentials,
                )

                async def maintain_resources():
                    nonlocal lease
                    while True:
                        await asyncio.sleep(min(30, max(1, settings.ttl_seconds // 4)))
                        async with recovery.store.transaction(recovery.lease):
                            pass
                        # Renewal preserves the same key; do not block the SQL
                        # heartbeat while an external control plane is slow.
                        if lease.metadata.expires_at <= time.time() + max(
                            60, settings.ttl_seconds // 4
                        ):
                            lease = await manager.admin.renew(lease)
                        await control.register_run(
                            run_id=run_id,
                            fence_token=fence,
                            frozen_config=frozen,
                            api_keys=credentials,
                        )
                        async with recovery.store.transaction(recovery.lease):
                            pass
                        from open_deep_research.agentscope_runtime.spend_reconciliation import (
                            collect_spend,
                        )

                        try:
                            async with asyncio.timeout(10):
                                await collect_spend(recovery.store, recovery.lease)
                        except Exception as exc:  # noqa: BLE001 -- retry failed external reconciliation on the next cycle
                            import logging

                            logging.getLogger(__name__).warning(
                                "Native spend reconciliation pending: %s",
                                type(exc).__name__,
                            )

                maintenance = asyncio.create_task(maintain_resources())
                owner_task = asyncio.current_task()

                def maintenance_done(task):
                    if not task.cancelled() and task.exception() is not None:
                        owner_task.cancel()

                maintenance.add_done_callback(maintenance_done)

                async def stop_maintenance():
                    maintenance.remove_done_callback(maintenance_done)
                    maintenance.cancel()
                    with suppress(asyncio.CancelledError):
                        await maintenance

                stack.push_async_callback(stop_maintenance)
            bindings = {
                role: CredentialBinding(
                    "gateway-capability", "run", run_id, tuple(names), SecretStr("")
                )
                for role in ROLES
            }
            models = ModelFactory(
                run_config, scope="run", owner=run_id, bindings=bindings
            )
            stack.push_async_callback(models.aclose)

            def task_identity(task_id):
                if worker_member_id:
                    from open_deep_research.agentscope_runtime.teams_worker import member_task
                    return member_task.get()
                return worker_task_id or (
                    "supervisor" if task_id == "pipeline" else task_id
                )

            def model_for(role, task_id):
                task_id = task_identity(task_id)
                from open_deep_research.agentscope_runtime.recovery_events import _STAGES
                return models.build_sandbox(
                    role,
                    SandboxBinding(
                        cfg.sandbox_gateway_url,
                        run_id,
                        task_id,
                        role,
                        lambda: _STAGES.get(recovery.stage.get().rsplit(":", 1)[0], "researching"),
                        lambda: token_for(task_id),
                    ),
                )

            async def tools_for(assignment, task_config=None):
                task_id = task_identity(getattr(assignment, "task_id", "supervisor"))
                await recovery.store.register_task(recovery.lease, task_id)
                scoped = {
                    **config,
                    "metadata": {
                        **config.get("metadata", {}),
                        "run_id": run_id,
                        "task_id": task_id,
                    },
                }
                tools = await load_gateway_catalog_tools(
                    "researcher",
                    scoped,
                    set(),
                    gateway_url=cfg.sandbox_gateway_url,
                    task_token=token_for(task_id),
                )
                from open_deep_research.tools.search_documents import search_documents

                if search_documents.is_enabled(scoped):
                    tools = [tool for tool in tools if tool.name != "search_documents"]
                    tools.append(
                        replace(
                            search_documents,
                            execution_zone=ToolExecutionZone.HOST_CONTROL,
                        )
                    )
                return tools

            async def dispatch(tool, input, context):
                if tool.execution_zone is not ToolExecutionZone.GATEWAY:
                    raise ValueError(
                        "trusted_team_worker_cannot_execute_sandbox_local_tools"
                    )
                task_id = task_identity(
                    context.config.get("metadata", {}).get("task_id", "supervisor")
                )
                return await GatewayToolProxy(
                    tool, cfg.sandbox_gateway_url, token_for(task_id)
                ).call(input, context)

            launcher = (
                launcher_factory()
                if launcher_factory
                and cfg.enable_async_research
                and worker_task_id is None
                and worker_member_id is None
                else None
            )
            if launcher:
                stack.push_async_callback(launcher.aclose)
            yield RunResources(
                models=models,
                tools_for=tools_for,
                model_for=model_for,
                dispatcher=dispatch,
                gateway_accounting=True,
                team_launcher=launcher,
                local_zones=frozenset({ToolExecutionZone.HOST_CONTROL}),
            )

    return open_resources
