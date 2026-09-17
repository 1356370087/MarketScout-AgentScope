"""Public usage revisions without importing legacy model callbacks."""

import logging

from open_deep_research.config_types import RuntimeConfig
from open_deep_research.events.public import event_publisher_from_config
from open_deep_research.observability.tracing import TraceRecorder

logger = logging.getLogger(__name__)


async def _publish_usage_revision(
    config: RuntimeConfig | None, revision: int | None, accounting_status: str
) -> None:
    metadata = (config or {}).get("metadata") or {}
    if not revision or not metadata.get("run_id"):
        return
    try:
        await event_publisher_from_config(config or {}).publish(
            "run.usage.updated",
            stage=None,
            payload={"revision": revision, "accounting_status": accounting_status},
            dedupe_key=f"run-usage:{revision}",
        )
    except Exception as exc:  # noqa: BLE001 - accounting is fail-open
        logger.debug("Usage update event failed open: %s", exc)


def _run_accounting_status(
    recorder: TraceRecorder,
    run_id: str,
    fallback: str = "partial",
) -> str:
    if recorder.store is None:
        return fallback
    projection = recorder._safe(recorder.store.get_usage_accounting, run_id) or {}
    return str(projection.get("accounting_status") or fallback)
