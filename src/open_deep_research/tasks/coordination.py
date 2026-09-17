"""Mailbox adapters for task events, Lead updates, and domain decisions."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from open_deep_research.config_types import RuntimeConfig

from open_deep_research.configuration import Configuration
from open_deep_research.tasks.events import EventType
from open_deep_research.tasks.mailbox import (
    CoordinationError,
    FileMailbox,
    MailboxMessage,
    validate_component,
)
from open_deep_research.tasks.state import TaskSnapshot, get_task_state_store

LEAD_AGENT_ID = "lead"


def _verify_result_artifact(
    configurable: Configuration,
    run_id: str,
    snapshot: TaskSnapshot,
) -> None:
    """Verify that a completed result exists inside the run and matches its digest."""
    if not snapshot.result_artifact_path or not snapshot.result_artifact_sha256:
        raise CoordinationError(f"Task {snapshot.task_id} completion is missing its result artifact")
    run_root = (Path(configurable.runs_dir).resolve() / validate_component(run_id, "run_id")).resolve()
    artifact = (run_root / snapshot.result_artifact_path).resolve()
    if run_root not in artifact.parents:
        raise CoordinationError(f"Task {snapshot.task_id} artifact escapes the run directory")
    try:
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    except OSError as exc:
        raise CoordinationError(f"Task {snapshot.task_id} result artifact is unavailable") from exc
    if digest != snapshot.result_artifact_sha256:
        raise CoordinationError(f"Task {snapshot.task_id} result artifact hash mismatch")


def get_run_id(config: RuntimeConfig) -> str:
    """Return the run identifier used to isolate coordination state."""
    return str(config.get("metadata", {}).get("run_id", "default"))


def get_mailbox(configurable: Configuration, run_id: str) -> FileMailbox:
    """Construct a mailbox using the run's coordination settings."""
    if configurable.enable_async_research and configurable.task_state_backend != "memory":
        from open_deep_research.tasks.team_inbox import TeamInbox
        return TeamInbox(run_id)
    return FileMailbox(
        runs_dir=configurable.runs_dir,
        run_id=run_id,
        lock_timeout_seconds=configurable.mailbox_lock_timeout_seconds,
        claim_lease_seconds=configurable.mailbox_claim_lease_seconds,
        max_delivery_attempts=configurable.mailbox_max_delivery_attempts,
        acked_retention_seconds=configurable.mailbox_acked_retention_seconds,
        compaction_threshold=configurable.mailbox_compaction_threshold,
    )


def _event_message_type(event_type: EventType | str) -> str:
    value = event_type.value if isinstance(event_type, EventType) else str(event_type)
    suffix = value.rsplit(".", 1)[-1]
    return {
        "started": "task_started",
        "completed": "task_completed",
        "failed": "task_failed",
        "cancelled": "task_cancelled",
        "timed_out": "task_timed_out",
        "domain_confirmation_requested": "domain_approval_request",
    }.get(suffix, "task_progress")


async def publish_task_update(
    configurable: Configuration,
    snapshot: TaskSnapshot,
    event_type: EventType | str,
) -> MailboxMessage:
    """Notify the Lead after the authoritative snapshot has been committed."""
    if configurable.enable_async_research and configurable.task_state_backend != "memory":
        # The PostgreSQL snapshot transaction already published this update.
        return MailboxMessage(
            run_id=snapshot.run_id, sender=snapshot.assigned_teammate_id or "lead",
            recipient=LEAD_AGENT_ID, type=_event_message_type(event_type),
            payload={"task_id": snapshot.task_id, "snapshot_version": snapshot.version},
        )
    mailbox = get_mailbox(configurable, snapshot.run_id)
    message_type = _event_message_type(event_type)
    priority = 0 if message_type in {"task_failed", "task_cancelled", "domain_approval_request"} else 40
    return await mailbox.send(
        recipient=LEAD_AGENT_ID,
        sender=snapshot.assigned_teammate_id or "orchestrator",
        message_type=message_type,
        priority=priority,
        dedupe_key=f"{snapshot.task_id}:{message_type}:{snapshot.version}",
        payload={
            "task_id": snapshot.task_id,
            "snapshot_version": snapshot.version,
            "status": snapshot.status.value,
            "phase": snapshot.phase.value,
            "artifact_path": snapshot.result_artifact_path,
            "artifact_sha256": snapshot.result_artifact_sha256,
            "pending_domain": snapshot.pending_domain,
        },
    )


async def claim_lead_update_messages(
    configurable: Configuration,
    *,
    run_id: str,
    consumer_id: str,
    timeout_seconds: float = 0,
) -> list[MailboxMessage]:
    """Claim pending Lead mailbox messages without rendering them."""
    mailbox = get_mailbox(configurable, run_id)
    if timeout_seconds > 0:
        return await mailbox.wait_and_claim(
            agent_id=LEAD_AGENT_ID,
            consumer_id=consumer_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=configurable.mailbox_poll_interval_ms / 1000,
        )
    return await mailbox.claim(agent_id=LEAD_AGENT_ID, consumer_id=consumer_id)


_VERDICT_ITEM_LIMIT = 4
_VERDICT_ITEM_MAX_CHARS = 240


def _quality_verdict_block(verdict: Mapping[str, Any]) -> str:
    """Render a bounded Supervisor-facing quality gate verdict.

    The verdict is a plain mapping produced by the admission layer; read it
    defensively so coordination never depends on quality-gate model types.
    """
    if not isinstance(verdict, Mapping):
        return ""
    status = str(verdict.get("admission_status", "") or "").strip()
    if not status:
        return ""

    def bounded_items(key: str) -> list[str]:
        values = verdict.get(key)
        if not isinstance(values, Sequence) or isinstance(values, str | bytes):
            return []
        return [
            str(item).strip()[:_VERDICT_ITEM_MAX_CHARS]
            for item in values[:_VERDICT_ITEM_LIMIT]
            if str(item).strip()
        ]

    lines = [f"Quality Gate: {status}"]
    if status == "accepted_with_caveats":
        lines.append(f"Caveats: {verdict.get('caveat_count', 0)}")
    reasons = bounded_items("hard_rejection_reasons")
    if reasons:
        lines.append("Rejection reasons: " + "; ".join(reasons))
    for label, key in (
        ("Missing information", "missing_information"),
        ("Follow-up tasks", "follow_up_tasks"),
    ):
        items = bounded_items(key)
        if items:
            lines.append(f"{label}:")
            lines.extend(f"- {item}" for item in items)
    return "\n".join(lines)


async def render_lead_update_context(
    configurable: Configuration,
    *,
    run_id: str,
    messages: list[MailboxMessage],
    processed_message_ids: set[str] | None = None,
    quality_verdicts: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    """Render claimed Lead messages into the Supervisor digest."""
    store = get_task_state_store(configurable)
    parts: list[str] = []
    processed = processed_message_ids or set()
    verdicts = quality_verdicts or {}
    for message in messages:
        if message.message_id in processed:
            continue
        task_id = str(message.payload.get("task_id", ""))
        snapshot = await store.get(task_id, run_id=run_id) if task_id else None
        if snapshot is None:
            parts.append(f"{message.type}: sender={message.sender}; payload={message.payload}")
            continue
        if message.type == "task_completed":
            _verify_result_artifact(configurable, run_id, snapshot)
        result_text = ""
        if snapshot.result and snapshot.status.value == "completed":
            result_text = str(snapshot.result.get("compressed_research", ""))
        verdict_text = _quality_verdict_block(verdicts.get(snapshot.task_id, {}))
        verdict_line = f"{verdict_text}\n" if verdict_text else ""
        parts.append(
            f"### {snapshot.task_id} - {snapshot.status.value.upper()}\n"
            f"Teammate: {snapshot.assigned_teammate_id or '(unassigned)'}\n"
            f"Topic: {snapshot.research_topic}\n"
            f"Version: {snapshot.version}\n"
            f"{verdict_line}"
            f"{result_text}"
        )
    return "Mailbox task updates:\n\n" + "\n---\n".join(parts) if parts else ""


async def claim_lead_updates(
    configurable: Configuration,
    *,
    run_id: str,
    consumer_id: str,
    timeout_seconds: float = 0,
    processed_message_ids: set[str] | None = None,
) -> tuple[list[MailboxMessage], str]:
    """Claim Lead messages and render their authoritative task snapshots."""
    messages = await claim_lead_update_messages(
        configurable,
        run_id=run_id,
        consumer_id=consumer_id,
        timeout_seconds=timeout_seconds,
    )
    if not messages:
        return [], ""
    context = await render_lead_update_context(
        configurable,
        run_id=run_id,
        messages=messages,
        processed_message_ids=processed_message_ids,
    )
    return messages, context


async def ack_lead_updates(
    configurable: Configuration,
    *,
    run_id: str,
    consumer_id: str,
    message_ids: list[str],
) -> None:
    """ACK Lead updates after their Supervisor state delta is durable."""
    if message_ids:
        await get_mailbox(configurable, run_id).ack(
            agent_id=LEAD_AGENT_ID,
            consumer_id=consumer_id,
            message_ids=message_ids,
        )
