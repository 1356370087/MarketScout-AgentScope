"""HTTP request identity and bounded body handling."""

from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from open_deep_research.configuration import Configuration
from open_deep_research.documents.settings import get_document_settings
from open_deep_research.logging_config import bind_request_id


async def request_id_middleware(request: Request, call_next: Any) -> Response:
    """Bind and echo a gateway request ID for logs, runs, and traces."""
    supplied = str(request.headers.get("X-Request-ID") or "").strip()
    request_id = (
        supplied
        if 0 < len(supplied) <= 128
        and re.fullmatch(r"[A-Za-z0-9._:-]+", supplied)
        else str(uuid.uuid4())
    )
    bind_request_id(request_id)
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


async def request_body_limit_middleware(request: Request, call_next: Any) -> Response:
    """Reject declared and streaming request bodies above the configured cap."""
    if request.method not in {"POST", "PUT", "PATCH"}:
        return await call_next(request)
    is_document_upload = request.method == "POST" and request.url.path.rstrip("/") == "/documents"
    limit = (
        get_document_settings().max_file_bytes + 2 * 1024 * 1024
        if is_document_upload
        else Configuration.from_runnable_config(None).max_request_body_bytes
    )
    content_length = request.headers.get("Content-Length")
    if is_document_upload and content_length is None:
        return JSONResponse({"detail": "content_length_required"}, status_code=411)
    if content_length is not None:
        try:
            if int(content_length) > limit:
                return JSONResponse(
                    {"detail": "request_body_too_large"},
                    status_code=413,
                )
        except ValueError:
            return JSONResponse({"detail": "invalid_content_length"}, status_code=400)
    if is_document_upload:
        # Content-Length is required before Starlette parses and spools multipart
        # data. stage_upload remains the per-file authoritative safety boundary.
        return await call_next(request)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            return JSONResponse(
                {"detail": "request_body_too_large"},
                status_code=413,
            )
    request._body = bytes(body)  # noqa: SLF001 - Starlette replays the bounded body
    return await call_next(request)
