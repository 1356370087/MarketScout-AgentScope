"""Publication HTTP compatibility over the durable domain publisher.

The host supplies ownership and connection lifecycle; publication rules live here.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

import portalocker
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from open_deep_research.api.contracts import PublicationRequest
from open_deep_research.api.streams import _sse_headers
from open_deep_research.configuration import Configuration
from open_deep_research.events.publications import (
    PublicationEventStore,
    publication_event_payload,
)
from open_deep_research.report.models import PublisherTheme
from open_deep_research.report.publication_store import (
    PublicationJob,
    PublicationJobStore,
    get_publisher_settings,
    worker_available,
)
from open_deep_research.run_context import RunContextStore, JournalCorruptedError
from security.rbac import Principal, require_permissions
from security.rbac.permissions import RESEARCH_RUN_READ_OWN

logger = logging.getLogger(__name__)


def _publication_response(job: PublicationJob) -> dict[str, Any]:
    """Return one publication with API URLs and no local path."""
    payload = job.public_dict()
    payload["status_url"] = f"/runs/{job.run_id}/publications/{job.publication_id}"
    payload["events_url"] = f"/runs/{job.run_id}/publications/events"
    payload["download_url"] = (
        f"/runs/{job.run_id}/publications/{job.publication_id}/download"
        if job.status == "completed" and job.artifact is not None
        else None
    )
    if isinstance(payload.get("artifact"), dict):
        payload["artifact"]["download_url"] = payload["download_url"]
    return payload


async def _run_publications(run_id: str, runs_dir: str) -> list[dict[str, Any]]:
    """Load a bounded publication list for a run snapshot."""
    try:
        store = PublicationJobStore(run_id, runs_dir=runs_dir)
        jobs = (await asyncio.to_thread(store.list))[:100]
    except OSError, ValueError, portalocker.exceptions.LockException:
        return []
    return [_publication_response(job) for job in jobs]


def _default_publication_theme(manifest: Any) -> PublisherTheme:
    """Load a persisted run theme, falling back to the bounded defaults."""
    try:
        return PublisherTheme.model_validate(manifest.publication_theme or {})
    except Exception:
        return PublisherTheme()


def _publication_job_or_404(
    store: PublicationJobStore,
    publication_id: str,
) -> PublicationJob:
    try:
        job = store.get(publication_id)
    except portalocker.exceptions.LockException:
        raise HTTPException(status_code=503, detail="publication_store_busy") from None
    except OSError, ValueError:
        raise HTTPException(status_code=404, detail="publication_not_found") from None
    if job is None:
        raise HTTPException(status_code=404, detail="publication_not_found")
    return job


class PublicationRoutes:
    """Bind publication commands to the application ownership and SSE services."""

    def __init__(
        self,
        *,
        require_run_owner,
        reserve_sse_connection,
        limited_sse,
        publication_event_iterator,
    ):
        self._require_run_owner = require_run_owner
        self._reserve_sse_connection = reserve_sse_connection
        self._limited_sse = limited_sse
        self._publication_event_iterator = publication_event_iterator
        self.router = APIRouter(tags=["publications"])
        self.router.add_api_route(
            "/runs/{run_id}/publications", self.create_publication, methods=["POST"]
        )
        self.router.add_api_route(
            "/runs/{run_id}/publications", self.list_publications, methods=["GET"]
        )
        self.router.add_api_route(
            "/runs/{run_id}/publications/events",
            self.stream_publication_events,
            methods=["GET"],
        )
        self.router.add_api_route(
            "/runs/{run_id}/publications/{publication_id}",
            self.publication_status,
            methods=["GET"],
        )
        self.router.add_api_route(
            "/runs/{run_id}/publications/{publication_id}/retry",
            self.retry_publication,
            methods=["POST"],
        )
        self.router.add_api_route(
            "/runs/{run_id}/publications/{publication_id}/download",
            self.download_publication,
            methods=["GET"],
        )

    def _publication_context(
        self,
        run_id: str,
        user: Principal,
    ) -> tuple[Configuration, Any, PublicationJobStore]:
        """Authorize a run and return its durable publication context."""
        record, configurable = self._require_run_owner(run_id, user)
        context = (
            getattr(record.engine, "context_store", None)
            if record is not None
            else None
        )
        if context is None:
            context = RunContextStore(run_id, runs_dir=configurable.runs_dir)
        try:
            manifest = context.load_manifest()
        except (
            JournalCorruptedError,
            OSError,
            ValueError,
            portalocker.exceptions.LockException,
        ):
            raise HTTPException(
                status_code=409,
                detail="publication_requires_persisted_run",
            ) from None
        return (
            configurable,
            manifest,
            PublicationJobStore(run_id, runs_dir=configurable.runs_dir),
        )

    async def create_publication(
        self,
        run_id: str,
        request: PublicationRequest,
        user: Principal = Depends(require_permissions(RESEARCH_RUN_READ_OWN.code)),
    ) -> Response:
        """Queue one owner-scoped report publication without changing Run status."""
        settings = get_publisher_settings()
        configurable, manifest, store = self._publication_context(run_id, user)
        if not settings.enabled:
            raise HTTPException(status_code=503, detail="publisher_disabled")
        if manifest.status not in {"completed", "success"}:
            raise HTTPException(status_code=409, detail="run_not_completed")
        try:
            markdown = store.load_final_report()
        except OSError, ValueError, portalocker.exceptions.LockException:
            raise HTTPException(
                status_code=409, detail="final_report_unavailable"
            ) from None
        if not markdown:
            raise HTTPException(status_code=409, detail="final_report_unavailable")
        if len(markdown) > settings.max_input_chars:
            raise HTTPException(status_code=413, detail="publication_input_too_large")
        report_sha256 = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        theme = request.theme or _default_publication_theme(manifest)

        def append_queued_event(job: PublicationJob) -> None:
            """Append the queue event while the Job lock is still held."""
            try:
                PublicationEventStore(
                    run_id,
                    runs_dir=configurable.runs_dir,
                ).append(
                    "publication.queued",
                    publication_id=job.publication_id,
                    payload=publication_event_payload(job),
                    dedupe_key=f"{job.publication_id}:publication.queued:0",
                )
            except Exception as exc:  # job status remains authoritative
                logger.warning(
                    "publication queued event failed run_id=%s publication_id=%s error_type=%s",
                    run_id,
                    job.publication_id,
                    type(exc).__name__,
                )

        try:
            job, created = await asyncio.to_thread(
                store.enqueue,
                report_sha256=report_sha256,
                publication_format=request.format,
                theme=theme,
                max_attempts=settings.max_attempts,
                on_created=append_queued_event,
            )
        except OSError, portalocker.exceptions.LockException:
            raise HTTPException(
                status_code=503, detail="publication_store_busy"
            ) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        response = _publication_response(job)
        response["reused"] = not created
        status_code = 202 if job.status in {"queued", "running"} else 200
        return JSONResponse(response, status_code=status_code)

    async def list_publications(
        self,
        run_id: str,
        user: Principal = Depends(require_permissions(RESEARCH_RUN_READ_OWN.code)),
    ) -> dict[str, Any]:
        """List durable publication jobs for one owned run."""
        _configurable, _manifest, store = self._publication_context(run_id, user)
        jobs = await asyncio.to_thread(store.list)
        default_theme = _default_publication_theme(_manifest)
        return {
            "run_id": run_id,
            "items": [_publication_response(job) for job in jobs[:100]],
            "events_url": f"/runs/{run_id}/publications/events",
            "default_theme": default_theme.model_dump(mode="json"),
            "worker": (
                "ready" if worker_available(get_publisher_settings()) else "degraded"
            ),
        }

    async def stream_publication_events(
        self,
        run_id: str,
        after: int = 0,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        user: Principal = Depends(require_permissions(RESEARCH_RUN_READ_OWN.code)),
    ) -> StreamingResponse:
        """Replay and tail the independent publication lifecycle stream."""
        configurable, _manifest, _job_store = self._publication_context(run_id, user)
        store = PublicationEventStore(run_id, runs_dir=configurable.runs_dir)
        cursor = after
        if last_event_id is not None:
            try:
                cursor = int(last_event_id)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail="invalid_publication_event_cursor",
                ) from None
        if cursor < 0:
            raise HTTPException(
                status_code=400,
                detail="invalid_publication_event_cursor",
            )
        try:
            current = await asyncio.to_thread(store.last_sequence)
        except OSError, ValueError, portalocker.exceptions.LockException:
            raise HTTPException(
                status_code=503, detail="publication_store_busy"
            ) from None
        if cursor > current:
            raise HTTPException(
                status_code=409, detail="publication_event_cursor_ahead"
            )
        release_token = await self._reserve_sse_connection(user, configurable)
        return StreamingResponse(
            self._limited_sse(
                self._publication_event_iterator(store, after=cursor, principal=user),
                release_token,
            ),
            media_type="text/event-stream",
            headers=_sse_headers(),
        )

    async def publication_status(
        self,
        run_id: str,
        publication_id: str,
        user: Principal = Depends(require_permissions(RESEARCH_RUN_READ_OWN.code)),
    ) -> dict[str, Any]:
        """Return one publication job without exposing its storage path."""
        _configurable, _manifest, store = self._publication_context(run_id, user)
        return _publication_response(_publication_job_or_404(store, publication_id))

    async def retry_publication(
        self,
        run_id: str,
        publication_id: str,
        user: Principal = Depends(require_permissions(RESEARCH_RUN_READ_OWN.code)),
    ) -> Response:
        """Requeue a retryable failed publication."""
        configurable, _manifest, store = self._publication_context(run_id, user)
        _publication_job_or_404(store, publication_id)

        def append_requeued_event(job: PublicationJob) -> None:
            """Append the requeue event before another worker can claim the Job."""
            try:
                PublicationEventStore(
                    run_id,
                    runs_dir=configurable.runs_dir,
                ).append(
                    "publication.requeued",
                    publication_id=job.publication_id,
                    payload=publication_event_payload(job),
                    dedupe_key=f"{job.publication_id}:manual-requeue:{job.attempt}",
                )
            except Exception:
                pass

        try:
            job = await asyncio.to_thread(
                store.retry,
                publication_id,
                on_requeued=append_requeued_event,
            )
        except FileNotFoundError:
            raise HTTPException(
                status_code=404, detail="publication_not_found"
            ) from None
        except OSError, portalocker.exceptions.LockException:
            raise HTTPException(
                status_code=503, detail="publication_store_busy"
            ) from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse(_publication_response(job), status_code=202)

    async def download_publication(
        self,
        run_id: str,
        publication_id: str,
        user: Principal = Depends(require_permissions(RESEARCH_RUN_READ_OWN.code)),
    ) -> FileResponse:
        """Download one completed, hash-verified publication."""
        _configurable, _manifest, store = self._publication_context(run_id, user)
        job = _publication_job_or_404(store, publication_id)
        if job.status != "completed" or job.artifact is None:
            raise HTTPException(status_code=409, detail="publication_not_ready")
        try:
            path = await asyncio.to_thread(store.artifact_path, job, verify=True)
        except FileNotFoundError:
            raise HTTPException(
                status_code=404,
                detail="publication_artifact_missing",
            ) from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OSError, portalocker.exceptions.LockException:
            raise HTTPException(
                status_code=503, detail="publication_store_busy"
            ) from None
        return FileResponse(
            path,
            media_type=job.artifact.media_type,
            filename=job.artifact.filename,
            headers={
                "ETag": f'"{job.artifact.sha256}"',
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, no-store",
            },
        )
