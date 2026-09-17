"""PostgreSQL-leased local document ingestion worker."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import socket
import time
import uuid

from prometheus_client import start_http_server

from open_deep_research.agentscope_runtime.documents import prepare_document
from open_deep_research.configuration import Configuration
from open_deep_research.observability.telemetry import get_prometheus_metrics

from . import reparse, versioning
from .embeddings import close_embedding_clients, embed_texts
from .metadata_suggest import build_suggestions
from .parse_pipeline import parse_document_structured
from .parsers import DocumentParseError
from .repository import (
    claim_job,
    complete_job,
    delete_if_unreferenced,
    fail_job,
    heartbeat,
    load_job_document,
    record_job_upstream_task,
    renew_job_lease,
)
from .settings import get_document_settings
from .storage import delete_storage_key, resolve_storage_key

logger = logging.getLogger(__name__)


class DocumentLeaseLostError(RuntimeError):
    """Raised when another worker has taken ownership of a durable job."""


async def _worker_heartbeat(worker_id: str, interval_seconds: float) -> None:
    """Publish process liveness independently of polling and ingestion duration."""
    interval = max(0.1, interval_seconds)
    while True:
        await heartbeat(worker_id)
        await asyncio.sleep(interval)


async def _maintain_job_lease(
    job_id: str,
    worker_id: str,
    *,
    interval_seconds: float,
    lease_seconds: int,
    lease_lost: asyncio.Event,
) -> None:
    """Renew a claimed job lease until completion or ownership loss."""
    interval = max(0.1, min(interval_seconds, max(1.0, lease_seconds / 3)))
    while True:
        await asyncio.sleep(interval)
        if not await renew_job_lease(job_id, worker_id, lease_seconds):
            lease_lost.set()
            logger.warning("document job lease lost job_id=%s", job_id)
            return


async def _process(worker_id: str) -> bool:
    settings = get_document_settings()
    job = await claim_job(worker_id, settings.worker_lease_seconds)
    if not job:
        return False
    started = time.perf_counter()
    outcome = "success"
    failure_code: str | None = None
    lease_lost = asyncio.Event()
    lease_task = asyncio.create_task(
        _maintain_job_lease(
            str(job["id"]),
            worker_id,
            interval_seconds=settings.worker_heartbeat_seconds,
            lease_seconds=settings.worker_lease_seconds,
            lease_lost=lease_lost,
        )
    )
    try:
        document = await load_job_document(job)
        if not document:
            await complete_job(str(job["id"]), worker_id)
            return True
        if job["kind"] == "delete":
            storage_key = await delete_if_unreferenced(str(document["id"]))
            if storage_key:
                delete_storage_key(storage_key, settings)
            await complete_job(str(job["id"]), worker_id)
            return True
        generation = await versioning.latest_draft_generation(str(document["id"]))
        if not generation:
            raise DocumentParseError("no_draft_generation")

        async def persist_task(task_id: str) -> None:
            await record_job_upstream_task(str(job["id"]), worker_id, task_id)

        if job["kind"] == "reparse":
            if not generation.get("reparse_scope"):
                raise DocumentParseError("reparse_scope_missing")
            await reparse.execute_scoped_reparse(
                dict(document), generation, settings, on_submitted=persist_task
            )
            if not await complete_job(str(job["id"]), worker_id):
                raise DocumentLeaseLostError("document_job_lease_lost")
            return True

        path = resolve_storage_key(document["storage_key"], settings)
        resume_task_id = (
            str(job["upstream_task_id"])
            if "upstream_task_id" in job.keys() and job["upstream_task_id"]
            else None
        )
        prepared = await prepare_document(
            path,
            document["filename"],
            document["media_type"],
            settings,
            parse_impl=parse_document_structured,
            version_id=str(generation["version_id"]),
            resume_task_id=resume_task_id,
            on_submitted=persist_task,
        )
        vectors = await embed_texts(prepared.segment_texts, settings)
        if lease_lost.is_set():
            raise DocumentLeaseLostError("document_job_lease_lost")
        suggestions = await build_suggestions(
            str(document["owner_id"]),
            prepared.units,
            document["filename"],
            settings,
        )
        await versioning.complete_generation_rich(
            str(generation["id"]),
            prepared,
            vectors,
            embedding_model=settings.embedding_model,
            metadata_suggestions=suggestions,
        )
        if not await complete_job(str(job["id"]), worker_id):
            raise DocumentLeaseLostError("document_job_lease_lost")
    except DocumentLeaseLostError:
        outcome = "lease_lost"
        logger.warning("discarding result after document job lease loss job_id=%s", job["id"])
    except Exception as exc:
        code = str(exc).split(":", 1)[0][:96] or "document_ingestion_failed"
        outcome = "failed"
        failure_code = code
        logger.exception(
            "document ingestion failed document_id=%s code=%s", job["document_id"], code
        )
        await fail_job(job, code, settings.worker_max_attempts, worker_id)
        if job["kind"] != "delete":
            await versioning.fail_generation_attempt(str(job["document_id"]), code)
    finally:
        lease_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await lease_task
        metrics = get_prometheus_metrics(Configuration.from_runnable_config(None))
        if metrics is not None:
            metrics.observe_document_job(
                str(job["kind"]),
                outcome,
                time.perf_counter() - started,
                failure_code=failure_code,
            )
    return True


async def run_worker(*, once: bool = False) -> None:
    """Run the durable worker loop or process at most one claim."""
    settings = get_document_settings()
    if not settings.configured:
        raise RuntimeError("document_research_not_configured")
    worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:12]}"
    metrics = get_prometheus_metrics(Configuration.from_runnable_config(None))
    if metrics is not None and settings.worker_metrics_port > 0:
        start_http_server(settings.worker_metrics_port, addr="0.0.0.0")
    heartbeat_task = asyncio.create_task(
        _worker_heartbeat(worker_id, settings.worker_heartbeat_seconds)
    )
    try:
        while True:
            worked = await _process(worker_id)
            if once:
                return
            if not worked:
                await asyncio.sleep(settings.worker_poll_seconds)
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        await close_embedding_clients()


def main() -> None:
    """Run the document worker CLI."""
    parser = argparse.ArgumentParser(
        description="InsightForge document ingestion worker"
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    asyncio.run(run_worker(once=args.once))


if __name__ == "__main__":
    main()
