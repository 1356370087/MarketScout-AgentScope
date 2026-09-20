"""Liveness, dependency readiness and process metrics endpoints."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from open_deep_research.api_host.run_retention import _runs_dir_size_bytes
from open_deep_research.configuration import Configuration
from open_deep_research.documents.database import document_operational_snapshot
from open_deep_research.documents.settings import get_document_settings
from open_deep_research.models.circuit import get_model_circuit_registry
from open_deep_research.models.credentials import RunKeySettings
from open_deep_research.observability import SQLiteTraceStore
from open_deep_research.observability.telemetry import get_prometheus_metrics
from security.rbac import check_database_connection
from security.rbac.settings import get_settings as get_iam_settings

logger = logging.getLogger(__name__)


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


class OperationalRoutes:
    """Expose operational state from the application lifecycle and runtime."""

    def __init__(
        self,
        *,
        native_service: Callable[[], Any],
        shutting_down: asyncio.Event,
        metrics_path: str,
    ) -> None:
        self.native_service = native_service
        self.shutting_down = shutting_down
        self.router = APIRouter()
        self.router.add_api_route(metrics_path, self.prometheus_metrics, methods=["GET"], include_in_schema=False)
        self.router.add_api_route("/healthz", healthz, methods=["GET"], include_in_schema=False)
        self.router.add_api_route("/readyz", self.readyz, methods=["GET"], include_in_schema=False)

    async def prometheus_metrics(self) -> Response:
        """Expose process-wide aggregate metrics for Prometheus scraping."""
        await _refresh_operational_metrics()
        body = generate_latest()
        configurable = Configuration.from_runnable_config(None)
        native_service = self.native_service()
        if (native_service is not None
                and configurable.observability_enabled and configurable.prometheus_enabled):
            from open_deep_research.agentscope_runtime.telemetry import prometheus_snapshot
            body += await prometheus_snapshot(native_service.store)
        return Response(content=body, media_type=CONTENT_TYPE_LATEST)

    async def _readiness_report(self) -> tuple[dict[str, Any], bool]:
        """Probe critical local dependencies and non-critical search credentials."""
        configurable = Configuration.from_runnable_config(None)
        components: dict[str, dict[str, Any]] = {}
        critical_ok = True

        if self.shutting_down.is_set():
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

    async def readyz(self) -> JSONResponse:
        """Report whether this process can safely receive new traffic."""
        report, ready = await self._readiness_report()
        return JSONResponse(report, status_code=200 if ready else 503)
