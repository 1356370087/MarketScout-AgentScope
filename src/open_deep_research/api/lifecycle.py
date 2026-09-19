"""Application startup checks, maintenance tasks and graceful shutdown."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI

from open_deep_research.configuration import Configuration
from open_deep_research.documents.database import close_document_pool, initialize_document_schema
from open_deep_research.documents.embeddings import close_embedding_clients
from open_deep_research.documents.settings import get_document_settings
from open_deep_research.models.credentials import RunKeySettings
from open_deep_research.models.key_reconciler import run_key_reconciler_loop
from security.rbac import shutdown_rbac, startup_checks
from security.rbac.app_extension import StartupError, assert_schema_current

logger = logging.getLogger(__name__)


class ApplicationLifecycle:
    """Own maintenance tasks and coordinate the host's runtime services."""

    def __init__(
        self,
        *,
        get_native_service: Callable[[], Any],
        set_native_service: Callable[[Any], None],
        recovery_sweep: Callable[[Configuration], Awaitable[int]],
        retention_loop: Callable[[Configuration], Awaitable[None]],
        native_key_cleanup: Callable[..., Awaitable[bool]],
        drain_runs: Callable[[float], Awaitable[None]],
        eviction_tasks: Callable[[], dict[str, asyncio.Task]],
    ) -> None:
        self.get_native_service = get_native_service
        self.set_native_service = set_native_service
        self.recovery_sweep = recovery_sweep
        self.retention_loop = retention_loop
        self.native_key_cleanup = native_key_cleanup
        self.drain_runs = drain_runs
        self.eviction_tasks = eviction_tasks
        self.shutting_down = asyncio.Event()
        self.sse_shutdown = asyncio.Event()
        self.retention_task: asyncio.Task | None = None
        self.key_reconciler_task: asyncio.Task | None = None

    @contextlib.asynccontextmanager
    async def lifespan(self, app: FastAPI):
        """Run startup recovery and gracefully interrupt live work on shutdown."""
        self.shutting_down.clear()
        self.sse_shutdown.clear()
        await startup_checks()
        from open_deep_research.agentscope_runtime.native_host import (
            native_engine_enabled,
        )

        if native_engine_enabled():
            from open_deep_research.agentscope_runtime.native_host import (
                build_native_research_service,
                mount_native_research,
            )

            native_service = await build_native_research_service(
                runs_dir=Configuration.from_runnable_config(None).runs_dir,
                database_url=os.getenv("AS_RECOVERY_DATABASE_URL") or None,
            )
            self.set_native_service(native_service)
            mount_native_research(app, native_service)
            logger.info(
                "RESEARCH_ENGINE=native: research run routes served by the native "
                "runtime; legacy runs are read-only history"
            )
        document_schema_error: str | None = None
        if get_document_settings().enabled:
            try:
                await assert_schema_current("0017_agent_teams")
            except StartupError as exc:
                if not str(exc).startswith("schema_revision_mismatch:"):
                    raise
                document_schema_error = await initialize_document_schema(str(exc))
            else:
                document_schema_error = await initialize_document_schema()
        else:
            try:
                await assert_schema_current("0017_agent_teams")
            except StartupError as exc:
                expected_old_revision = (
                    "schema_revision_mismatch:got=0012_facts:"
                    "expected=0017_agent_teams"
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
                await self.recovery_sweep(configurable)
            except Exception as exc:  # noqa: BLE001 - recovery is fail-open
                logger.warning("run recovery sweep failed: %s", exc)
        if configurable.retention_sweep_interval_seconds > 0:
            self.retention_task = asyncio.create_task(
                self.retention_loop(configurable)
            )
        if configurable.model_backend == "litellm":
            key_settings = RunKeySettings.from_env()
            self.key_reconciler_task = asyncio.create_task(
                run_key_reconciler_loop(
                    key_settings,
                    native_cleanup=self.native_key_cleanup,
                    runs_dir=configurable.runs_dir,
                    interval_seconds=float(
                        os.getenv("LITELLM_RUN_KEY_RECONCILE_INTERVAL_SECONDS", "300")
                    ),
                )
            )
        try:
            yield
        finally:
            self.shutting_down.set()
            try:
                await self.drain_runs(
                    configurable.shutdown_drain_timeout_seconds
                )
            finally:
                self.sse_shutdown.set()
                native_service = self.get_native_service()
                if native_service is not None:
                    await native_service.native_aclose()
                    self.set_native_service(None)
                if self.retention_task is not None:
                    self.retention_task.cancel()
                    await asyncio.gather(self.retention_task, return_exceptions=True)
                    self.retention_task = None
                if self.key_reconciler_task is not None:
                    self.key_reconciler_task.cancel()
                    await asyncio.gather(
                        self.key_reconciler_task,
                        return_exceptions=True,
                    )
                    self.key_reconciler_task = None
                for task in list(self.eviction_tasks().values()):
                    task.cancel()
                if self.eviction_tasks():
                    await asyncio.gather(
                        *self.eviction_tasks().values(),
                        return_exceptions=True,
                    )
                self.eviction_tasks().clear()
                from open_deep_research.tasks.team_runtime import team_runtime
                await team_runtime.close()
                await close_document_pool()
                await close_embedding_clients()
                await shutdown_rbac()
