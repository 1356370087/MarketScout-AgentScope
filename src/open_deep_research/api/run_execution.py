"""Background research execution and durable control-command consumption."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from open_deep_research.api.contracts import RunRequest
from open_deep_research.api.run_registry import RunRecord
from open_deep_research.configuration import Configuration
from open_deep_research.events.public import event_publisher_from_config
from open_deep_research.run_control import RunControlStore

logger = logging.getLogger(__name__)


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


class RunExecution:
    """Execute runs and hand terminal records to the process registry."""

    def __init__(self, schedule_eviction: Callable[[RunRecord, dict[str, Any] | None], None]) -> None:
        self.schedule_eviction = schedule_eviction

    async def _run_background(self, record: RunRecord, request: RunRequest, config: dict[str, Any]) -> None:
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
            self.schedule_eviction(record, config)

    async def _run_resumed_background(self, record: RunRecord) -> None:
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
            self.schedule_eviction(record, config)
