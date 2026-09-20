"""Fenced startup recovery of orphaned persisted research runs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import Any

from open_deep_research.api_host.run_retention import _TERMINAL_RUN_STATUSES, _load_manifests_from_root
from open_deep_research.configuration import Configuration
from open_deep_research.events.public import event_publisher_from_config
from open_deep_research.observability import get_trace_recorder
from open_deep_research.run_context import RunContextStore
from open_deep_research.tasks.lease import LeaderLeaseManager, LeaseConflictError

logger = logging.getLogger(__name__)


def _runs_root(config: dict[str, Any] | None = None) -> Path:
    return Path(Configuration.from_runnable_config(config).runs_dir).resolve()


def _load_manifests(config: dict[str, Any] | None = None) -> list[Any]:
    return _load_manifests_from_root(_runs_root(config))


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
