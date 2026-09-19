"""Run artifact deletion and periodic retention, independent of HTTP startup."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from open_deep_research.configuration import Configuration
from open_deep_research.documents.repository import release_run_sources
from open_deep_research.documents.settings import get_document_settings
from open_deep_research.observability import SQLiteTraceStore
from open_deep_research.observability.telemetry import get_prometheus_metrics
from open_deep_research.run_context import JournalCorruptedError, RunContextStore
from open_deep_research.run_control import RunControlStore
from open_deep_research.tasks.lease import LeaderLeaseManager, LeaseConflictError
from security.rbac import Principal, require_permissions
from security.rbac.permissions import RESEARCH_RUN_CONTROL_OWN

logger = logging.getLogger(__name__)
_TERMINAL_RUN_STATUSES = frozenset({"success", "completed", "failed", "interrupted", "cancelled"})


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


async def _cancel_before_forced_purge(
    run_id: str,
    record: Any,
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


class RunRetention:
    """Use the host registry while owning the retention and purge algorithms."""

    def __init__(self, *, runs, eviction_tasks, require_run_owner):
        self.runs = runs
        self.eviction_tasks = eviction_tasks
        self.require_run_owner = require_run_owner
        self.router = APIRouter()
        self.router.add_api_route("/runs/{run_id}", self.delete_run, methods=["DELETE"])

    async def _purge_run_artifacts(
        self,
        run_id: str,
        configurable: Configuration,
        *,
        reason: str,
        actor: str,
        require_terminal: bool = True,
    ) -> dict[str, Any]:
        """Delete disk, trace, and memory state through one idempotent path."""
        target = _run_directory(configurable, run_id)
        record = self.runs().get(run_id)
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
        eviction_task = self.eviction_tasks().pop(run_id, None)
        if eviction_task is not None:
            eviction_task.cancel()
        self.runs().pop(run_id, None)
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


    async def _run_retention_sweep(self, configurable: Configuration) -> dict[str, Any]:
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
                        await self._purge_run_artifacts(
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
                            await self._purge_run_artifacts(
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


    async def _retention_sweep_loop(self, configurable: Configuration) -> None:
        """Run lifecycle cleanup periodically until service shutdown."""
        interval = configurable.retention_sweep_interval_seconds
        while True:
            await asyncio.sleep(interval)
            try:
                await self._run_retention_sweep(configurable)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - maintenance remains fail-open
                _record_lifecycle_error(configurable, "retention_sweep", exc)


    async def delete_run(
        self,
        run_id: str,
        force: bool = False,
        dry_run: bool = False,
        user: Principal = Depends(require_permissions(RESEARCH_RUN_CONTROL_OWN.code)),
    ) -> dict[str, Any]:
        """Permanently delete an owned run and all durable observability rows."""
        is_admin = "admin" in user.roles
        if is_admin:
            record = self.runs().get(run_id)
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
            record, configurable = self.require_run_owner(run_id, user)
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
        return await self._purge_run_artifacts(
            run_id,
            configurable,
            reason="manual",
            actor=user.user_id,
            require_terminal=not force,
        )


