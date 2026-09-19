"""Persisted run listing and snapshot queries for the legacy host."""

from __future__ import annotations

import base64
import json
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from open_deep_research.api.projections import _stable_output
from open_deep_research.api.publication_routes import _run_publications
from open_deep_research.configuration import Configuration
from open_deep_research.events.public import RunEventStore
from open_deep_research.run_context import JournalCorruptedError, RunContextStore
from security.rbac import Principal, require_permissions
from security.rbac.permissions import RESEARCH_RUN_READ_OWN


def _encode_cursor(created_at: float, run_id: str) -> str:
    raw = json.dumps([created_at, run_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[float, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode())
        return float(value[0]), str(value[1])
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid_cursor") from exc


class RunReadRoutes:
    """Read the host's records without owning scheduling or lifecycle state."""

    def __init__(self, *, load_manifests, lookup_record, require_record_owner, augment_projection):
        self.load_manifests = load_manifests
        self.lookup_record = lookup_record
        self.require_record_owner = require_record_owner
        self.augment_projection = augment_projection
        self.router = APIRouter()
        self.router.add_api_route("/runs", self.list_runs, methods=["GET"])
        self.router.add_api_route("/runs/{run_id}", self.get_run, methods=["GET"])

    async def list_runs(
        self,
        limit: int = 30,
        cursor: str | None = None,
        status: str | None = None,
        user: Principal = Depends(require_permissions(RESEARCH_RUN_READ_OWN.code)),
    ) -> dict[str, Any]:
        """List the authenticated user's persisted run manifests newest first."""
        if limit < 1 or limit > 100:
            raise HTTPException(status_code=422, detail="limit_must_be_between_1_and_100")
        owner = user.user_id
        manifests = [
            item
            for item in self.load_manifests()
            if item.owner_id == owner
            and (status is None or item.status == status)
        ]
        manifests.sort(key=lambda item: (item.created_at, item.run_id), reverse=True)
        if cursor:
            cursor_key = _decode_cursor(cursor)
            manifests = [
                item for item in manifests if (item.created_at, item.run_id) < cursor_key
            ]
        page = manifests[: limit + 1]
        has_more = len(page) > limit
        page = page[:limit]
        items = [
            {
                "run_id": item.run_id,
                "title": item.title or item.run_id,
                "query_preview": item.query_preview or item.run_id,
                "status": item.status,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
                "last_event_id": item.last_public_event_seq,
            }
            for item in page
        ]
        next_cursor = (
            _encode_cursor(page[-1].created_at, page[-1].run_id)
            if has_more and page
            else None
        )
        return {"items": items, "next_cursor": next_cursor}


    async def get_run(
        self,
        run_id: str,
        user: Principal = Depends(require_permissions(RESEARCH_RUN_READ_OWN.code)),
    ) -> dict[str, Any]:
        """Return the latest status/result for a run."""
        record = self.lookup_record(run_id)
        if record is None:
            configurable = Configuration.from_runnable_config(None)
            try:
                store = RunContextStore(run_id, runs_dir=configurable.runs_dir)
                manifest = store.load_manifest()
            except (ValueError, JournalCorruptedError, OSError):
                raise HTTPException(status_code=404, detail="Run not found") from None
            if not manifest.owner_id or manifest.owner_id != user.user_id:
                raise HTTPException(status_code=404, detail="Run not found")
            result = manifest.result
            report_text = ""
            if manifest.status in {"completed", "success"}:
                report_path = store.context_dir / "final_report.md"
                if report_path.exists():
                    report_text = report_path.read_text(encoding="utf-8")
                    # Preserve a partial completion status and quality metadata
                    # from the durable manifest while restoring the Markdown body.
                    restored_result = dict(result or {}) if isinstance(result, dict) else {}
                    restored_result.setdefault("status", "success")
                    restored_result["result"] = report_text
                    result = restored_result
            event_store = RunEventStore(run_id, runs_dir=configurable.runs_dir)
            projection = event_store.project() if event_store.exists else None
            projection = self.augment_projection(run_id, projection, configurable)
            stored_configurable = (
                dict(manifest.config.get("configurable") or {})
                if isinstance(manifest.config, dict)
                else {}
            )
            output = _stable_output(
                manifest.result,
                report_text,
                publications=await _run_publications(run_id, configurable.runs_dir),
                preferred_output_format=stored_configurable.get("output_format"),
                publication_theme=manifest.publication_theme,
            )
            if output.get("report_review") is None and projection is not None:
                output["report_review"] = projection.report_review or None
            return {
                "run_id": run_id,
                "title": manifest.title or run_id,
                "status": manifest.status,
                "created_at": manifest.created_at,
                "updated_at": manifest.updated_at,
                "runtime_seconds": max(0.0, manifest.updated_at - manifest.created_at),
                "pending_human_action": manifest.pending_human_action,
                "pending_security_approvals": (
                    projection.pending_security_approvals if projection else []
                ),
                "result": result,
                "output": output,
                "event_count": manifest.last_journal_seq,
                "persistence_degraded": manifest.persistence_degraded,
                "progress": projection.model_dump() if projection else None,
                "events_url": f"/runs/{run_id}/events",
                "last_event_id": projection.last_event_id if projection else 0,
            }
        self.require_record_owner(record, user)
        configurable = Configuration.from_runnable_config(record.engine.config)
        event_store = RunEventStore(run_id, runs_dir=configurable.runs_dir)
        projection = event_store.project()
        projection = self.augment_projection(run_id, projection, configurable)
        manifest = (
            record.engine.context_store.load_manifest()
            if getattr(record.engine, "context_store", None)
            and record.engine.context_store.manifest_path.exists()
            else None
        )
        now = time.time()
        manifest_status = manifest.status if manifest is not None else None
        if (
            manifest_status in {"completed", "failed", "cancelled", "interrupted"}
            and record.status != manifest_status
        ):
            # The durable manifest is authoritative once terminal; the in-memory
            # record can lag behind a just-finished run.
            run_status = manifest_status
        else:
            run_status = record.status
        # The engine publishes the terminal SSE event before stream consumption
        # copies final_state into RunRecord. Serve that already-committed result
        # even when this request races the background consumer (also on resume).
        result = record.result
        if getattr(record.engine, "status", None) in {"completed", "failed", "cancelled"}:
            result = getattr(record.engine, "final_state", None) or result
            run_status = record.engine.status
        output = _stable_output(
            result,
            publications=await _run_publications(run_id, configurable.runs_dir),
            preferred_output_format=getattr(configurable, "output_format", None),
            publication_theme=(
                manifest.publication_theme
                if manifest is not None
                else dict(
                    getattr(record.engine, "config", {})
                    .get("metadata", {})
                    .get("publication_theme")
                    or {}
                )
            ),
        )
        if output.get("report_review") is None and projection.report_review:
            output["report_review"] = projection.report_review
        return {
            "run_id": run_id,
            "title": (manifest.title if manifest else None) or run_id,
            "status": run_status,
            "created_at": manifest.created_at if manifest else record.engine.started_at,
            "updated_at": manifest.updated_at if manifest else now,
            "runtime_seconds": max(0.0, now - record.engine.started_at),
            "pending_human_action": (
                getattr(record.engine, "pending_human_action", None)
                or (manifest.pending_human_action if manifest else None)
                or projection.pending_human_action
            ),
            "pending_security_approvals": projection.pending_security_approvals,
            "result": result,
            "output": output,
            "event_count": projection.last_event_id,
            "progress": projection.model_dump(),
            "events_url": f"/runs/{run_id}/events",
            "last_event_id": projection.last_event_id,
        }


