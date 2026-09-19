"""Web-page sync: conditional fetch, change detection, version creation (KB-10).

Each sync source binds a URL to a logical document. The adapter protocol
(``validate_config → discover_changes → fetch_snapshot → checkpoint``) lets
future platforms (Feishu, SharePoint, Notion) plug in behind the same
interface; only the web adapter exists today. Conditional requests avoid
re-downloading unchanged pages; a body-hash check decides whether to create
a new document version that flows into the existing parse-and-review
pipeline. One pending draft per source prevents daily refreshes from
stacking up behind an unreviewed update.
"""

from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from open_deep_research.documents.database import get_document_pool
from open_deep_research.documents.identity import document_owner_id
from open_deep_research.documents.repository import DocumentConflictError

REFRESH_INTERVALS = {"manual": None, "daily": timedelta(days=1), "weekly": timedelta(weeks=1)}
MAX_RETRIES = 3
USER_AGENT = "InsightForge-KB/1.0 (+sync)"


class SyncError(RuntimeError):
    """Raised when a sync operation fails (network, config, or state)."""


@dataclass(slots=True)
class SyncSnapshot:
    """One fetch result ready to become a document version or a no-op."""

    body: bytes
    final_url: str
    etag: str | None = None
    last_modified: str | None = None
    content_hash: str = ""
    changed: bool = True
    status: int = 200
    extracted_text: str = ""


@dataclass(slots=True)
class SyncSourceRecord:
    """One row from knowledge_sync_sources, decoded for the adapter."""

    id: str
    document_id: str
    knowledge_base_id: str
    input_url: str
    normalized_url: str
    refresh_mode: str
    etag: str | None
    last_modified: str | None
    content_hash: str
    paused: bool
    consecutive_failures: int
    next_run_at: datetime | None = None
    last_success_at: datetime | None = None


class SourceAdapter(ABC):
    """Protocol for external-source adapters (plan §KB-10)."""

    @abstractmethod
    async def validate_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Validate the source configuration and return a normalized form."""

    @abstractmethod
    async def discover_changes(
        self, source: SyncSourceRecord
    ) -> SyncSnapshot | None:
        """Fetch the current snapshot; return None if nothing changed."""

    @abstractmethod
    async def fetch_snapshot(self, source: SyncSourceRecord) -> SyncSnapshot:
        """Fetch the full current content (for initial import)."""

    @abstractmethod
    async def checkpoint(
        self, source_id: str, snapshot: SyncSnapshot, *, success: bool
    ) -> None:
        """Persist the fetch state (ETag, hash, failure counter)."""


def normalize_sync_url(raw: str) -> str:
    """Normalize a URL for dedup: lowercase scheme/host, strip fragments."""
    parts = urlsplit(raw.strip())
    return urlunsplit((
        parts.scheme.lower(), parts.netloc.lower(),
        parts.path or "/", parts.query, "",
    ))


def _extract_visible_text(html: bytes) -> str:
    """Extract visible text from HTML (no scripts, styles, or tags)."""
    text = html.decode("utf-8", errors="replace")
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


class WebAdapter(SourceAdapter):
    """Plain-web adapter: conditional GET, no login, no JS rendering."""

    def __init__(self, authorize_url=None):
        self.authorize_url = authorize_url

    async def _get(self, client, url, headers):
        if self.authorize_url is None:
            return await client.get(url, headers=headers)
        for _ in range(6):
            await self.authorize_url(url)
            response = await client.get(url, headers=headers, follow_redirects=False)
            if not response.is_redirect:
                return response
            location = response.headers.get("location")
            if not location:
                raise SyncError("sync_redirect_missing_location")
            url = str(response.url.join(location))
        raise SyncError("sync_too_many_redirects")

    async def validate_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Validate the URL and return its normalized form."""
        url = str(config.get("url") or "").strip()
        normalized = normalize_sync_url(url)
        parts = urlsplit(normalized)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise SyncError("sync_url_invalid")
        return {"normalized_url": normalized, "input_url": url}

    async def discover_changes(
        self, source: SyncSourceRecord
    ) -> SyncSnapshot | None:
        """Issue a conditional GET; return None for 304 or unchanged hash."""
        headers = {"User-Agent": USER_AGENT}
        if source.etag:
            headers["If-None-Match"] = source.etag
        if source.last_modified:
            headers["If-Modified-Since"] = source.last_modified
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                response = await self._get(client, source.input_url, headers)
        except httpx.TimeoutException as exc:
            raise SyncError("sync_timeout") from exc
        except (httpx.HTTPError, OSError) as exc:
            raise SyncError("sync_unreachable") from exc
        if response.status_code == 304:
            return None
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "60")
            raise SyncError(f"sync_rate_limited:{retry_after}")
        if response.status_code >= 500:
            raise SyncError(f"sync_server_error:{response.status_code}")
        if response.status_code >= 400:
            raise SyncError(f"sync_http_error:{response.status_code}")
        extracted = _extract_visible_text(response.content)
        content_hash = hashlib.sha256(extracted.encode()).hexdigest()
        if content_hash == source.content_hash:
            return None  # Body unchanged, just update the checkpoint
        return SyncSnapshot(
            body=response.content,
            final_url=str(response.url),
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
            content_hash=content_hash,
            extracted_text=extracted,
        )

    async def fetch_snapshot(self, source: SyncSourceRecord) -> SyncSnapshot:
        """Unconditional GET for the initial import."""
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                response = await self._get(client, source.input_url, {"User-Agent": USER_AGENT})
        except (httpx.HTTPError, OSError) as exc:
            raise SyncError("sync_unreachable") from exc
        if response.status_code >= 400:
            raise SyncError(f"sync_http_error:{response.status_code}")
        extracted = _extract_visible_text(response.content)
        return SyncSnapshot(
            body=response.content,
            final_url=str(response.url),
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
            content_hash=hashlib.sha256(extracted.encode()).hexdigest(),
            extracted_text=extracted,
        )

    async def checkpoint(
        self, source_id: str, snapshot: SyncSnapshot | None, *, success: bool
    ) -> None:
        """Persist fetch state; bump or reset the failure counter."""
        pool = await get_document_pool()
        async with pool.acquire() as connection:
            if success and snapshot:
                interval = REFRESH_INTERVALS.get(
                    (await connection.fetchval(
                        "SELECT refresh_mode FROM knowledge_sync_sources WHERE id=$1::uuid",
                        source_id,
                    )) or "daily"
                )
                await connection.execute(
                    """UPDATE knowledge_sync_sources
                          SET etag=$2, last_modified=$3, content_hash=$4,
                              last_success_at=now(),
                              next_run_at=$5, consecutive_failures=0, updated_at=now()
                        WHERE id=$1::uuid""",
                    source_id,
                    snapshot.etag,
                    snapshot.last_modified,
                    snapshot.content_hash,
                    datetime.now(timezone.utc) + interval if interval else None,
                )
            elif success:
                await connection.execute(
                    """UPDATE knowledge_sync_sources
                          SET last_success_at=now(), consecutive_failures=0, updated_at=now()
                        WHERE id=$1::uuid""",
                    source_id,
                )
            else:
                interval = REFRESH_INTERVALS.get(
                    (await connection.fetchval(
                        "SELECT refresh_mode FROM knowledge_sync_sources WHERE id=$1::uuid",
                        source_id,
                    )) or "daily"
                )
                backoff = min(3600, 60 * 2 ** min(5, snapshot and 0 or 1))
                await connection.execute(
                    """UPDATE knowledge_sync_sources
                          SET consecutive_failures=consecutive_failures+1,
                              next_run_at=now() + make_interval(secs => $2), updated_at=now()
                        WHERE id=$1::uuid""",
                    source_id,
                    backoff,
                )


async def create_sync_source(
    actor_id: str,
    knowledge_base_id: str,
    document_id: str,
    url: str,
    *,
    refresh_mode: str = "daily",
) -> dict[str, Any] | None:
    """Create one sync source binding a URL to a logical document."""
    actor_id = document_owner_id(actor_id)
    adapter = WebAdapter()
    config = await adapter.validate_config({"url": url})
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        pending = await connection.fetchval(
            """SELECT count(*) FROM research_document_generations
                WHERE document_id=$1::uuid AND status IN ('draft','pending_review')""",
            document_id,
        )
        if int(pending) > 0:
            raise DocumentConflictError("sync_pending_draft_exists")
        try:
            source_id = await connection.fetchval(
                """INSERT INTO knowledge_sync_sources
                     (knowledge_base_id, document_id, input_url, normalized_url,
                      refresh_mode, created_by)
                   VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6::uuid)
                 RETURNING id""",
                knowledge_base_id,
                document_id,
                config["input_url"],
                config["normalized_url"],
                refresh_mode,
                actor_id,
            )
        except Exception as exc:
            if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                raise DocumentConflictError("sync_source_already_exists") from exc
            raise
    return {"id": str(source_id), "normalized_url": config["normalized_url"]}


async def run_sync(actor_id: str, source_id: str, *, authorize_url=None) -> dict[str, Any]:
    """Execute one sync: fetch, detect change, optionally create a version.

    Returns ``{"status": "unchanged"|"updated"|"error", ...}``. Body changes
    flow into the existing parse-and-review pipeline as a new draft version;
    the previous published version keeps serving retrieval until the draft
    is human-published (plan KB-10 §6).
    """
    adapter = WebAdapter(authorize_url)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT * FROM knowledge_sync_sources WHERE id=$1::uuid", source_id
        )
    if not row:
        return {"status": "error", "code": "sync_source_not_found"}
    source = SyncSourceRecord(
        id=str(row["id"]),
        document_id=str(row["document_id"]),
        knowledge_base_id=str(row["knowledge_base_id"]),
        input_url=row["input_url"],
        normalized_url=row["normalized_url"],
        refresh_mode=row["refresh_mode"],
        etag=row["etag"],
        last_modified=row["last_modified"],
        content_hash=row["content_hash"] or "",
        paused=row["paused"],
        consecutive_failures=int(row["consecutive_failures"]),
    )
    if source.paused:
        return {"status": "skipped", "code": "sync_source_paused"}
    try:
        snapshot = await adapter.discover_changes(source)
    except SyncError as exc:
        await adapter.checkpoint(source_id, None, success=False)
        return {"status": "error", "code": str(exc)}
    if snapshot is None:
        await adapter.checkpoint(source_id, None, success=True)
        return {"status": "unchanged"}
    # Body changed: create a new version that enters the review pipeline.
    import tempfile
    from pathlib import Path

    from open_deep_research.documents.storage import StagedUpload
    from open_deep_research.documents.versioning import add_document_version

    fd, tmp_path = tempfile.mkstemp(suffix=".html")
    try:
        import os

        os.write(fd, snapshot.body)
        os.close(fd)
        staged = StagedUpload(
            Path(tmp_path), "sync.html", "text/html",
            len(snapshot.body), snapshot.content_hash,
        )
        try:
            result = await add_document_version(
                actor_id, source.document_id, staged,
                f"sync/{source_id[:8]}/{snapshot.content_hash[:12]}.html",
                note=f"Synced from {source.normalized_url}",
            )
        except DocumentConflictError as exc:
            # 待审核更新存在时暂停自动追加（方案 KB-10 单一待审草稿守卫）。
            await adapter.checkpoint(source_id, None, success=True)
            return {"status": "error", "code": str(exc)}
    finally:
        import os

        os.unlink(tmp_path)
    await adapter.checkpoint(source_id, snapshot, success=True)
    return {
        "status": "updated",
        "version": result["version"] if result else None,
        "generation": result["generation"] if result else None,
    }
