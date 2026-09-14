"""Independent file-queue worker for report publication jobs."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import signal
import tempfile
import time
from contextlib import suppress
from pathlib import Path

import portalocker

from open_deep_research.events.publications import (
    PublicationEventStore,
    publication_event_payload,
)
from open_deep_research.run_context import RunContextStore

from .canonical import (
    CanonicalizationLimits,
    canonicalize_report,
    validate_canonical_report,
)
from .models import CanonicalReport
from .publication_store import (
    PublicationJob,
    PublicationJobStore,
    PublisherSettings,
    claim_available_jobs,
    get_publisher_settings,
    new_worker_id,
    worker_available,
)
from .publishers import (
    PublicationRenderError,
    render_publication,
    validate_rendered_artifact,
)
from .references import parse_sources_from_text

logger = logging.getLogger(__name__)


def _atomic_heartbeat(path: Path, worker_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"worker_id": worker_id, "timestamp": time.time()},
        separators=(",", ":"),
    ).encode("utf-8")
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _job_download_url(job: PublicationJob) -> str:
    return (
        f"/runs/{job.run_id}/publications/"
        f"{job.publication_id}/download"
    )


def _reconcile_terminal_events(settings: PublisherSettings) -> int:
    """Backfill terminal events once when a worker process starts.

    Job JSON is the authoritative state machine.  A process can still exit
    between ``store.complete``/``store.fail`` and the corresponding event
    append, so the next worker startup repairs that narrow window.  Each Run's
    event log is read once to avoid rescanning it for every historical Job.
    """
    if not settings.runs_dir.exists():
        return 0
    repaired = 0
    jobs_by_run: dict[str, list[Path]] = {}
    for path in settings.runs_dir.glob("*/context/publications/jobs/*.json"):
        jobs_by_run.setdefault(path.parents[3].name, []).append(path)
    for run_id, paths in sorted(jobs_by_run.items()):
        try:
            store = PublicationJobStore(run_id, runs_dir=settings.runs_dir)
            event_store = PublicationEventStore(run_id, runs_dir=settings.runs_dir)
            known_keys = {event.dedupe_key for event in event_store.read()}
        except (
            OSError,
            ValueError,
            RuntimeError,
            portalocker.exceptions.LockException,
        ) as exc:
            logger.debug(
                "publication terminal event reconciliation skipped run_id=%s error_type=%s",
                run_id,
                type(exc).__name__,
            )
            continue
        for path in sorted(paths):
            try:
                job = store._read_job(path)  # noqa: SLF001
                if job.status not in {"completed", "failed"}:
                    continue
                event_type = (
                    "publication.completed"
                    if job.status == "completed"
                    else "publication.failed"
                )
                dedupe_key = f"{job.publication_id}:{event_type}:{job.attempt}"
                if dedupe_key in known_keys:
                    continue
                event_store.append(
                    event_type,
                    publication_id=job.publication_id,
                    payload=publication_event_payload(
                        job,
                        download_url=(
                            _job_download_url(job)
                            if job.status == "completed"
                            else None
                        ),
                    ),
                    dedupe_key=dedupe_key,
                )
                known_keys.add(dedupe_key)
                repaired += 1
            except (
                OSError,
                ValueError,
                RuntimeError,
                portalocker.exceptions.LockException,
            ) as exc:
                logger.debug(
                    "publication terminal event reconciliation skipped run_id=%s "
                    "publication_id=%s error_type=%s",
                    run_id,
                    path.stem,
                    type(exc).__name__,
                )
    return repaired


def _load_canonical_report(
    store: PublicationJobStore,
    job: PublicationJob,
    settings: PublisherSettings,
) -> tuple[CanonicalReport, str]:
    try:
        markdown = store.load_final_report()
    except OSError as exc:
        raise PublicationRenderError(
            "publication_final_report_missing",
            retryable=False,
        ) from exc
    except UnicodeError as exc:
        # A persisted report is an untrusted file boundary.  Keep malformed
        # text/path failures distinguishable from a transient I/O failure so
        # callers get a stable, non-retryable public error code.
        raise PublicationRenderError(
            "publication_final_report_invalid",
            retryable=False,
        ) from exc
    except ValueError as exc:
        code = str(exc)
        if not code.startswith(("publication_", "canonical_report_")):
            code = "publication_final_report_invalid"
        raise PublicationRenderError(code, retryable=False) from exc
    if len(markdown) > settings.max_input_chars:
        raise PublicationRenderError("canonical_report_input_too_large")
    try:
        report_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    except UnicodeError as exc:
        raise PublicationRenderError(
            "publication_final_report_invalid",
            retryable=False,
        ) from exc
    if report_hash != job.report_sha256:
        raise PublicationRenderError("source_report_changed")

    canonical: CanonicalReport | None = None
    try:
        canonical_text = store.load_canonical_report()
    except FileNotFoundError:
        canonical_text = None
    except (OSError, ValueError, portalocker.exceptions.LockException):
        canonical_text = None
    # The Markdown budget is the authoritative input limit.  A canonical
    # bundle may contain metadata in addition to the body, but an unbounded
    # bundle should never be handed to Pydantic in the worker process.
    if (
        canonical_text is not None
        and len(canonical_text) <= settings.max_input_chars
    ):
        try:
            canonical = CanonicalReport.model_validate_json(
                canonical_text
            )
            canonical = validate_canonical_report(
                canonical,
                limits=CanonicalizationLimits(max_chars=settings.max_input_chars),
            )
        except Exception:
            canonical = None
    if (
        canonical is not None
        and canonical.run_id == job.run_id
        and canonical.source_markdown_sha256 == report_hash
    ):
        return canonical, markdown

    try:
        manifest = RunContextStore(
            job.run_id,
            runs_dir=str(settings.runs_dir),
        ).load_manifest()
        configurable = (
            dict(manifest.config.get("configurable") or {})
            if isinstance(manifest.config, dict)
            else {}
        )
        report_type = str(configurable.get("report_type") or "default")
        completion_status = (
            "partial"
            if isinstance(manifest.result, dict)
            and manifest.result.get("status") == "partial"
            else "success"
        )
        fallback_title = manifest.title or "Research Report"
    except Exception:
        report_type = "default"
        completion_status = "success"
        fallback_title = "Research Report"
    try:
        canonical = canonicalize_report(
            markdown,
            run_id=job.run_id,
            report_type=report_type,
            completion_status=completion_status,
            locale=job.theme.locale,
            sources=parse_sources_from_text(markdown),
            fallback_title=fallback_title,
            limits=CanonicalizationLimits(max_chars=settings.max_input_chars),
        )
        store.persist_canonical_report(canonical.model_dump(mode="json"))
    except PublicationRenderError:
        raise
    except Exception as exc:
        code = str(exc) if str(exc).startswith("canonical_report_") else "canonical_report_invalid"
        raise PublicationRenderError(code) from exc
    return canonical, markdown


class PublisherWorker:
    """Claim publication jobs, render them and commit durable results."""

    def __init__(
        self,
        settings: PublisherSettings | None = None,
        *,
        worker_id: str | None = None,
    ) -> None:
        """Initialize one worker with a stable process identity."""
        self.settings = settings or get_publisher_settings()
        self.worker_id = worker_id or new_worker_id()
        self._stop = asyncio.Event()
        self._last_heartbeat = 0.0
        self._startup_reconciled = False

    def stop(self) -> None:
        """Request graceful loop termination."""
        self._stop.set()

    async def _heartbeat(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if (
            not force
            and now - self._last_heartbeat
            < self.settings.heartbeat_interval_seconds
        ):
            return
        try:
            await asyncio.to_thread(
                _atomic_heartbeat,
                self.settings.heartbeat_path,
                self.worker_id,
            )
        except OSError as exc:
            logger.warning(
                "publisher heartbeat write failed worker_id=%s error_type=%s",
                self.worker_id,
                type(exc).__name__,
            )
            return
        self._last_heartbeat = now

    async def _publish_event(
        self,
        job: PublicationJob,
        event_type: str,
    ) -> None:
        try:
            store = PublicationEventStore(
                job.run_id,
                runs_dir=self.settings.runs_dir,
            )
            await asyncio.to_thread(
                store.append,
                event_type,
                publication_id=job.publication_id,
                payload=publication_event_payload(
                    job,
                    download_url=(
                        _job_download_url(job)
                        if job.status == "completed"
                        else None
                    ),
                ),
                dedupe_key=(
                    f"{job.publication_id}:{event_type}:{job.attempt}"
                ),
            )
        except Exception as exc:  # event delivery cannot corrupt the job state
            logger.warning(
                "publication event persistence failed run_id=%s publication_id=%s type=%s error_type=%s",
                job.run_id,
                job.publication_id,
                event_type,
                type(exc).__name__,
            )

    def _publish_exhausted_event(
        self,
        _store: PublicationJobStore,
        job: PublicationJob,
    ) -> None:
        """Record a failed event for a lease recovered after attempt exhaustion."""
        try:
            PublicationEventStore(
                job.run_id,
                runs_dir=self.settings.runs_dir,
            ).append(
                "publication.failed",
                publication_id=job.publication_id,
                payload=publication_event_payload(job),
                dedupe_key=(
                    f"{job.publication_id}:publication.failed:{job.attempt}"
                ),
            )
        except Exception as exc:  # job status remains authoritative
            logger.warning(
                "publication exhausted event persistence failed run_id=%s publication_id=%s error_type=%s",
                job.run_id,
                job.publication_id,
                type(exc).__name__,
            )

    async def _fail_claimed_job(
        self,
        store: PublicationJobStore,
        job: PublicationJob,
        *,
        error_code: str,
        retryable: bool,
    ) -> None:
        """Record a failure unless another worker already reclaimed the lease."""
        def append_failure_event(failed: PublicationJob, requeued: bool) -> None:
            """Write the terminal/requeue event before releasing the Job lock."""
            try:
                event_type = (
                    "publication.requeued" if requeued else "publication.failed"
                )
                PublicationEventStore(
                    failed.run_id,
                    runs_dir=self.settings.runs_dir,
                ).append(
                    event_type,
                    publication_id=failed.publication_id,
                    payload=publication_event_payload(failed),
                    dedupe_key=f"{failed.publication_id}:{event_type}:{failed.attempt}",
                )
            except Exception as exc:
                logger.warning(
                    "publication failure event persistence failed run_id=%s publication_id=%s error_type=%s",
                    failed.run_id,
                    failed.publication_id,
                    type(exc).__name__,
                )

        try:
            await asyncio.to_thread(
                store.fail,
                job.publication_id,
                worker_id=self.worker_id,
                error_code=error_code,
                retryable=retryable,
                on_updated=append_failure_event,
            )
        except RuntimeError as exc:
            if str(exc) == "publication_lease_lost":
                logger.info(
                    "publication lease lost run_id=%s publication_id=%s",
                    job.run_id,
                    job.publication_id,
                )
                return
            raise
        except (
            OSError,
            ValueError,
            portalocker.exceptions.LockException,
        ) as exc:
            logger.warning(
                "publication failure state could not be persisted run_id=%s publication_id=%s error_type=%s",
                job.run_id,
                job.publication_id,
                type(exc).__name__,
            )
            return
    async def _renew_lease_loop(
        self,
        store: PublicationJobStore,
        job: PublicationJob,
        stop: asyncio.Event,
    ) -> None:
        """Keep a long-running renderer fenced to this worker."""
        interval = max(
            0.5,
            min(
                30.0,
                self.settings.lease_seconds / 3,
                self.settings.heartbeat_interval_seconds,
            ),
        )
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                renewed = await asyncio.to_thread(
                    store.renew,
                    job.publication_id,
                    worker_id=self.worker_id,
                    lease_seconds=self.settings.lease_seconds,
                )
                await self._heartbeat()
                if not renewed:
                    return
            except (OSError, RuntimeError, ValueError, portalocker.exceptions.LockException):
                return

    async def _process_claimed(
        self,
        store: PublicationJobStore,
        job: PublicationJob,
    ) -> None:
        """Render one already-claimed job."""
        await self._publish_event(job, "publication.started")
        try:
            canonical, markdown = await asyncio.to_thread(
                _load_canonical_report,
                store,
                job,
                self.settings,
            )
            rendered = await asyncio.to_thread(
                render_publication,
                canonical,
                job.format,
                job.theme,
                source_markdown=markdown,
            )
            await asyncio.to_thread(
                validate_rendered_artifact,
                canonical,
                job.format,
                rendered,
                source_markdown=markdown,
                max_pdf_pages=self.settings.max_pdf_pages,
                max_pptx_slides=self.settings.max_pptx_slides,
            )
            if (
                rendered.page_count is not None
                and rendered.page_count > self.settings.max_pdf_pages
            ):
                raise PublicationRenderError(
                    "publisher_pdf_page_limit_exceeded"
                )
            if (
                rendered.slide_count is not None
                and rendered.slide_count > self.settings.max_pptx_slides
            ):
                raise PublicationRenderError("pptx_slide_limit_exceeded")
            artifact = await asyncio.to_thread(
                store.commit_file,
                job,
                rendered,
                report_title=canonical.title,
                max_output_bytes=self.settings.max_output_bytes,
                worker_id=self.worker_id,
            )
            completed = await asyncio.to_thread(
                store.complete,
                job.publication_id,
                worker_id=self.worker_id,
                artifact=artifact,
            )
            await self._publish_event(completed, "publication.completed")
            logger.info(
                "publication completed run_id=%s publication_id=%s format=%s size_bytes=%s",
                completed.run_id,
                completed.publication_id,
                completed.format,
                artifact.size_bytes,
            )
        except PublicationRenderError as exc:
            await self._fail_claimed_job(
                store,
                job,
                error_code=exc.code,
                retryable=exc.retryable,
            )
        except (OSError, TimeoutError) as exc:
            await self._fail_claimed_job(
                store,
                job,
                error_code="publication_io_error",
                retryable=True,
            )
            logger.warning(
                "publication I/O failure publication_id=%s error_type=%s",
                job.publication_id,
                type(exc).__name__,
            )
        except ValueError as exc:
            code = str(exc)
            if not code.startswith(("publication_", "canonical_report_")):
                code = "publication_render_failed"
            await self._fail_claimed_job(
                store,
                job,
                error_code=code,
                retryable=False,
            )
        except Exception as exc:  # noqa: BLE001 - normalize renderer faults
            await self._fail_claimed_job(
                store,
                job,
                error_code="publication_render_failed",
                retryable=False,
            )
            logger.exception(
                "publication failed publication_id=%s error_type=%s",
                job.publication_id,
                type(exc).__name__,
            )

    async def process(
        self,
        store: PublicationJobStore,
        job: PublicationJob,
    ) -> None:
        """Render one claimed job while renewing its lease."""
        stop = asyncio.Event()
        renewal = asyncio.create_task(self._renew_lease_loop(store, job, stop))
        try:
            await self._process_claimed(store, job)
        finally:
            stop.set()
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)

    async def run_once(self) -> int:
        """Claim and process one bounded worker batch."""
        if not self.settings.enabled:
            return 0
        if not self._startup_reconciled:
            await asyncio.to_thread(_reconcile_terminal_events, self.settings)
            self._startup_reconciled = True
        await self._heartbeat(force=True)
        claimed = await asyncio.to_thread(
            claim_available_jobs,
            self.settings,
            worker_id=self.worker_id,
            limit=self.settings.max_concurrent_jobs,
            on_exhausted=self._publish_exhausted_event,
        )
        if claimed:
            await asyncio.gather(
                *(self.process(store, job) for store, job in claimed),
                return_exceptions=True,
            )
        await self._heartbeat(force=True)
        return len(claimed)

    async def run_forever(self) -> None:
        """Poll the shared queue until shutdown."""
        if not self.settings.enabled:
            raise RuntimeError("publisher_disabled")
        logger.info(
            "publisher worker started worker_id=%s runs_dir=%s",
            self.worker_id,
            self.settings.runs_dir,
        )
        while not self._stop.is_set():
            count = await self.run_once()
            if count:
                continue
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self.settings.poll_interval_seconds,
                )
            except TimeoutError:
                await self._heartbeat()


def _install_signal_handlers(worker: PublisherWorker) -> None:
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        with suppress(NotImplementedError):
            loop.add_signal_handler(signum, worker.stop)


async def _run(args: argparse.Namespace) -> int:
    settings = get_publisher_settings()
    if args.healthcheck:
        return 0 if worker_available(settings) else 1
    worker = PublisherWorker(settings)
    _install_signal_handlers(worker)
    if args.once:
        await worker.run_once()
        return 0
    await worker.run_forever()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the worker CLI."""
    parser = argparse.ArgumentParser(description="InsightForge report publisher")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
