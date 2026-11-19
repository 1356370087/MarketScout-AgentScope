"""Client for the self-hosted Docling Serve asynchronous conversion API.

Implements the reliability contract from plan §KB-06: submit file bytes only
(never user-supplied URLs), persist the upstream task id so a restarted
worker resumes polling before resubmitting, and surface distinguishable
error codes for auth, availability, timeout and upstream failure.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from .parsers import DocumentParseError
from .settings import DocumentSettings

_TERMINAL_STATUSES = frozenset({"success", "partial_success", "failure", "skipped"})
_SUCCESS_STATUSES = frozenset({"success", "partial_success"})


class DoclingTaskLostError(RuntimeError):
    """Raised when the server no longer knows a persisted upstream task."""


def _client(settings: DocumentSettings, timeout: float) -> httpx.AsyncClient:
    # docling-serve's API-key middleware reads the X-API-Key header; the
    # Bearer scheme is rejected by the deployed v1.32 instance.
    headers = {"X-API-Key": settings.docling_api_key} if settings.docling_api_key else {}
    return httpx.AsyncClient(
        base_url=settings.docling_base_url, headers=headers, timeout=timeout
    )


def _form_options(
    settings: DocumentSettings, page_range: tuple[int, int] | None = None
) -> dict[str, Any]:
    options: dict[str, Any] = {
        "to_formats": "json",
        "do_ocr": "true",
        "do_table_structure": "true",
    }
    if settings.docling_ocr_lang:
        options["ocr_lang"] = settings.docling_ocr_lang
    if page_range:
        # The upstream field is a two-integer [start, end] tuple (1-based);
        # scoped re-parse sends neighbour pages for cross-page table context.
        options["page_range"] = [str(page_range[0]), str(page_range[1])]
    # Picture description stays local/off by deployment default: third-party
    # image-caption APIs are disabled unless an operator configures the server.
    return options


async def submit_file(
    path: Path,
    filename: str,
    media_type: str,
    settings: DocumentSettings,
    *,
    page_range: tuple[int, int] | None = None,
) -> str:
    """Upload one file for asynchronous conversion and return its task id."""
    payload = path.read_bytes()
    try:
        async with _client(settings, min(settings.docling_timeout_seconds, 120.0)) as client:
            response = await client.post(
                "/v1/convert/file/async",
                files={"files": (filename, payload, media_type or "application/octet-stream")},
                data=_form_options(settings, page_range),
            )
    except httpx.TimeoutException as exc:
        raise DocumentParseError("document_docling_submit_timeout") from exc
    except (httpx.HTTPError, OSError) as exc:
        raise DocumentParseError("document_docling_unavailable") from exc
    if response.status_code in {401, 403}:
        raise DocumentParseError("document_docling_unauthorized")
    if response.status_code == 413:
        raise DocumentParseError("document_docling_file_too_large")
    if response.status_code >= 400:
        raise DocumentParseError(f"document_docling_submit_http_{response.status_code}")
    task_id = (response.json() or {}).get("task_id")
    if not task_id:
        raise DocumentParseError("document_docling_invalid_task_response")
    return str(task_id)


async def poll_status(task_id: str, settings: DocumentSettings) -> dict:
    """Return the raw task status document."""
    try:
        async with _client(settings, 30.0) as client:
            response = await client.get(f"/v1/status/poll/{task_id}")
    except (httpx.HTTPError, OSError) as exc:
        raise DocumentParseError("document_docling_unavailable") from exc
    if response.status_code == 404:
        raise DoclingTaskLostError(task_id)
    if response.status_code >= 400:
        raise DocumentParseError(f"document_docling_status_http_{response.status_code}")
    return response.json() or {}


async def fetch_result(task_id: str, settings: DocumentSettings) -> dict:
    """Return the raw conversion result, mapping failures to parse errors."""
    try:
        async with _client(settings, 120.0) as client:
            response = await client.get(f"/v1/result/{task_id}")
    except (httpx.HTTPError, OSError) as exc:
        raise DocumentParseError("document_docling_unavailable") from exc
    if response.status_code == 404:
        raise DoclingTaskLostError(task_id)
    if response.status_code >= 400:
        raise DocumentParseError(f"document_docling_result_http_{response.status_code}")
    body = response.json()
    if isinstance(body, dict) and body.get("kind") == "TaskFailureResult":
        failure = body.get("failure") or {}
        category = str(failure.get("category") or "unknown")
        raise DocumentParseError(f"document_docling_task_failed:{category}")
    return body or {}


async def convert(
    path: Path,
    filename: str,
    media_type: str,
    settings: DocumentSettings,
    *,
    resume_task_id: str | None = None,
    on_submitted: Callable[[str], Awaitable[None]] | None = None,
    page_range: tuple[int, int] | None = None,
) -> tuple[str, dict]:
    """Run one conversion, resuming a persisted task when possible.

    Returns ``(upstream_task_id, result_body)``. ``on_submitted`` fires once
    the task id is known (after submit, or immediately when resuming) so the
    caller can persist it before the long poll. A lost upstream task is
    resubmitted exactly once; polling stops at terminal status or overall
    timeout, whichever comes first.
    """
    task_id = resume_task_id
    resumed = task_id is not None
    if not resumed:
        task_id = await submit_file(
            path, filename, media_type, settings, page_range=page_range
        )
    if on_submitted is not None:
        await on_submitted(task_id)
    deadline = time.monotonic() + settings.docling_timeout_seconds
    try:
        while True:
            status = await poll_status(task_id, settings)
            state = str(status.get("task_status") or "")
            if state == "failure":
                failure = status.get("failure") or {}
                category = str(failure.get("category") or "unknown")
                raise DocumentParseError(f"document_docling_task_failed:{category}")
            if state == "skipped":
                raise DocumentParseError("document_docling_task_skipped")
            if state in _SUCCESS_STATUSES:
                return task_id, await fetch_result(task_id, settings)
            if time.monotonic() >= deadline:
                raise DocumentParseError("document_docling_timeout")
            await asyncio.sleep(max(0.2, settings.docling_poll_seconds))
    except DoclingTaskLostError:
        if resumed:
            # The server lost the persisted task: resubmit exactly once.
            return await convert(
                path, filename, media_type, settings, resume_task_id=None,
                on_submitted=on_submitted, page_range=page_range,
            )
        raise DocumentParseError("document_docling_task_lost") from None
