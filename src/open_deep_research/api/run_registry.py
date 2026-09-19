"""Process-owned research records, bounded retention and shutdown draining."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from open_deep_research.api.run_retention import _TERMINAL_RUN_STATUSES
from open_deep_research.configuration import Configuration
from open_deep_research.events.public import event_publisher_from_config
from open_deep_research.observability import get_trace_recorder

if TYPE_CHECKING:
    from open_deep_research.agents.query_engine import QueryEngine

logger = logging.getLogger(__name__)


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


class RunRegistry:
    """Own active records and delayed eviction tasks for one server process."""

    def __init__(self):
        self.runs: dict[str, RunRecord] = {}
        self.eviction_tasks: dict[str, asyncio.Task[None]] = {}

    def _evict_run_record(
        self,
        run_id: str,
        *,
        expected_finished_at: float,
        reason: str,
    ) -> bool:
        """Evict the matching terminal record without touching a resumed replacement."""
        record = self.runs.get(run_id)
        if (
            record is None
            or record.status not in _TERMINAL_RUN_STATUSES
            or record.finished_at != expected_finished_at
        ):
            return False
        self.runs.pop(run_id, None)
        eviction_task = self.eviction_tasks.pop(run_id, None)
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


    def _enforce_run_memory_limit(self, maximum: int) -> None:
        """Evict least-recently-finished terminal runs until the soft cap is met."""
        while len(self.runs) > maximum:
            candidates = [
                record
                for record in self.runs.values()
                if record.status in _TERMINAL_RUN_STATUSES
                and record.finished_at is not None
            ]
            if not candidates:
                return
            oldest = min(candidates, key=lambda item: item.finished_at or 0.0)
            self._evict_run_record(
                oldest.run_id,
                expected_finished_at=oldest.finished_at or 0.0,
                reason="capacity",
            )


    def _remember_run(self, record: RunRecord, config: dict[str, Any] | None) -> None:
        """Register a live run and enforce the process-local record cap."""
        configurable = Configuration.from_runnable_config(config)
        if record.events.maxlen != configurable.inflight_event_buffer_size:
            record.events = deque(
                record.events,
                maxlen=configurable.inflight_event_buffer_size,
            )
        stale_task = self.eviction_tasks.pop(record.run_id, None)
        if stale_task is not None:
            stale_task.cancel()
        self.runs[record.run_id] = record
        self._enforce_run_memory_limit(configurable.max_inflight_runs_in_memory)


    async def _evict_run_after_delay(
        self,
        run_id: str,
        expected_finished_at: float,
        delay_seconds: float,
    ) -> None:
        """Evict a terminal run after its configured in-memory grace period."""
        try:
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            self._evict_run_record(
                run_id,
                expected_finished_at=expected_finished_at,
                reason="retention",
            )
        finally:
            try:
                current_task = asyncio.current_task()
            except RuntimeError:
                current_task = None
            if self.eviction_tasks.get(run_id) is current_task:
                self.eviction_tasks.pop(run_id, None)


    def _schedule_run_eviction(
        self,
        record: RunRecord,
        config: dict[str, Any] | None,
    ) -> None:
        """Mark a terminal record finished and schedule bounded retention."""
        if record.status not in _TERMINAL_RUN_STATUSES:
            return
        if record.finished_at is None:
            record.finished_at = time.time()
        configurable = Configuration.from_runnable_config(config)
        self._enforce_run_memory_limit(configurable.max_inflight_runs_in_memory)
        if self.runs.get(record.run_id) is not record:
            return
        previous = self.eviction_tasks.pop(record.run_id, None)
        if previous is not None:
            previous.cancel()
        self.eviction_tasks[record.run_id] = asyncio.create_task(
            self._evict_run_after_delay(
                record.run_id,
                record.finished_at,
                configurable.inflight_run_memory_retention_seconds,
            )
        )


    async def _drain_inflight_runs(self, timeout_seconds: float) -> None:
        """Interrupt all live in-memory runs within one shutdown budget."""
        records = [
            record
            for record in list(self.runs.values())
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


