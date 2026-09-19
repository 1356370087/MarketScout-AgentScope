"""Read legacy artifacts without loading an engine, repairing logs or writing locks."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from open_deep_research.api.projections import _stable_output
from open_deep_research.events.public import PublicEvent, project_public_events
from open_deep_research.run_context import RunManifest, SessionJournalRecord


class HistoricalRunReader:
    """Owner-scoped archive reader; historical checkpoints are never executable."""

    def __init__(self, runs_dir: str | Path, run_id: str, user_id: str):
        self.root = Path(runs_dir).resolve()
        self.run_dir = (self.root / run_id).resolve()
        if self.run_dir.parent != self.root:
            raise FileNotFoundError("Run not found")
        self.context = self.run_dir / "context"
        self.manifest = RunManifest.model_validate_json(
            self._path("context/manifest.json").read_text(encoding="utf-8")
        )
        if self.manifest.run_id != run_id or self.manifest.owner_id != user_id:
            raise FileNotFoundError("Run not found")
        self.run_id = run_id

    def _path(self, relative: str) -> Path:
        path = (self.run_dir / relative).resolve()
        if not path.is_relative_to(self.run_dir):
            raise ValueError("Historical artifact escapes run directory")
        return path

    def _records(self, relative, model, sequence_field):
        path = self._path(relative)
        if not path.exists():
            return []
        records = []
        # A damaged tail is reported, never truncated by a read request.
        for line in path.read_bytes().splitlines():
            if not line.strip():
                continue
            record = model.model_validate_json(line)
            if (
                record.run_id != self.run_id
                or getattr(record, sequence_field) != len(records) + 1
            ):
                raise ValueError("Historical log sequence mismatch")
            records.append(record)
        return records

    def events(self, after=0):
        """Read public events on their original cursor domain."""
        return [
            event
            for event in self._records("public_events.jsonl", PublicEvent, "sequence")
            if event.sequence > after
        ]

    def messages(self, channel="lead"):
        """Project the latest query snapshot or incremental legacy message journal."""
        from open_deep_research.agentscope_runtime.messages import read_legacy_messages

        messages = []
        for record in self._records(
            "context/session_memory.jsonl", SessionJournalRecord, "seq"
        ):
            if record.channel != channel:
                continue
            payload = record.payload
            if record.record_type == "query_state":
                messages = payload["state"].get("messages", [])
            elif (
                record.record_type == "context_compacted"
                and "recent_messages" in payload
            ):
                messages = payload.get("recent_messages", [])
            elif record.record_type in {
                "message_delta",
                "state_delta",
                "stage_checkpoint",
                "context_compacted",
            }:
                value = payload.get("update", {}).get(
                    "supervisor_messages" if channel == "supervisor" else "messages", []
                )
                if isinstance(value, dict) and value.get("type") == "override":
                    messages = value["value"]
                else:
                    messages = [*messages, *value]
        decoded = []
        for message in messages:
            if "__message_artifact__" in message:
                relative = "context/" + message["__message_artifact__"]
                message = json.loads(self._path(relative).read_text(encoding="utf-8"))[
                    "message"
                ]
            decoded.append(message.get("__message__", message))
        return read_legacy_messages(decoded)

    def budget(self):
        """Return recorded accounting facts; missing usage is not zero usage."""
        path = self._path("context/budget_ledger.json")
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def publication_events(self):
        """Validate archived publication events without repairing their tail."""
        from open_deep_research.events.publications import PublicationEventStore

        return PublicationEventStore(self.run_id, runs_dir=self.root).read(read_only=True)

    def usage(self, trace_path, unavailable):
        """Project persisted usage without schema migration, backfill or reconciliation."""
        from open_deep_research.observability.tracing import SQLiteTraceStore

        budget = self.budget() or {}
        reserved = {}
        for item in budget.get("reservations", {}).values():
            if item.get("status") in {"reserved", "uncertain"}:
                dimension = item["dimension"]
                amount = item.get("actual")
                reserved[dimension] = reserved.get(dimension, 0) + int(
                    item["reserved"] if amount is None else amount
                )
        try:
            response = SQLiteTraceStore(trace_path, read_only=True).get_usage_accounting(
                self.run_id, reserved_budget=reserved
            )
        except (OSError, sqlite3.Error):
            response = unavailable
        response["status"] = self.manifest.status
        response["duration_ms"] = max(
            0, int((self.manifest.updated_at - self.manifest.created_at) * 1000)
        )
        spend = self.manifest.litellm_spend_micro_usd
        if spend is not None:
            response["totals"]["cost"] = {
                "estimated_cost_micro_usd": spend,
                "cost_source": "stale_gateway_snapshot",
                "price_table_hash": None,
            }
            response["cost_source"] = "stale_gateway_snapshot"
            response["totals"]["budgets"]["cost_micro_usd"].update(
                settled=spend, estimated=0, reserved=0
            )
        return response

    def snapshot(self):
        """Browser DTO without persisted credentials, configuration or checkpoint state."""
        manifest = self.manifest
        projection = project_public_events(self.events())
        report = ""
        path = self._path("context/final_report.md")
        if manifest.status in {"completed", "success"} and path.exists():
            report = path.read_text(encoding="utf-8")
        result = manifest.result or {}
        return {
            "run_id": self.run_id,
            "title": manifest.title or self.run_id,
            "status": manifest.status,
            "created_at": manifest.created_at,
            "updated_at": manifest.updated_at,
            "pending_human_action": manifest.pending_human_action,
            "pending_security_approvals": projection.pending_security_approvals,
            "progress": projection.model_dump(mode="json"),
            "output": {**_stable_output(result, report), "markdown": report},
            "last_event_id": projection.last_event_id,
            "events_url": f"/runs/{self.run_id}/events",
            "engine": "legacy",
            "read_only": True,
            "resumable": False,
        }


def build_history_router(runs_dir, principal_dependency):
    """Compose with an authenticated read-permission dependency in the new host.

    Install only for archive dispatch; the active engine owns native run routes.
    Identity must come from IAM, never a caller-supplied user header.
    """
    from fastapi import APIRouter, Depends, Header, HTTPException
    from fastapi.responses import StreamingResponse

    from open_deep_research.api.streams import _sse, _sse_headers

    router = APIRouter()
    authenticated = Depends(principal_dependency)
    event_cursor = Header(default=None, alias="Last-Event-ID")

    def reader(run_id, principal):
        try:
            return HistoricalRunReader(runs_dir, run_id, principal.user_id)
        except FileNotFoundError, OSError:
            raise HTTPException(404, "Run not found") from None
        except ValueError:
            raise HTTPException(409, "historical_artifact_corrupted") from None

    @router.get("/runs/{run_id}")
    def get_run(run_id: str, principal=authenticated):
        try:
            return reader(run_id, principal).snapshot()
        except OSError, ValueError:
            raise HTTPException(409, "historical_artifact_corrupted") from None

    @router.post("/runs/{run_id}/resume")
    def resume_run(run_id: str, principal=authenticated):
        reader(run_id, principal)
        raise HTTPException(409, "legacy_checkpoint_read_only")

    @router.get("/runs/{run_id}/events")
    def events(
        run_id: str,
        after: int = 0,
        last_event_id: str | None = event_cursor,
        principal=authenticated,
    ):
        archive = reader(run_id, principal)
        if not archive._path("public_events.jsonl").exists():
            raise HTTPException(409, "event_stream_unavailable_legacy_run")
        try:
            cursor = int(last_event_id) if last_event_id is not None else after
        except ValueError:
            raise HTTPException(400, "invalid_event_cursor") from None
        if cursor < 0:
            raise HTTPException(400, "invalid_event_cursor")
        try:
            records = archive.events()
        except OSError, ValueError:
            raise HTTPException(409, "historical_artifact_corrupted") from None
        if cursor > (records[-1].sequence if records else 0):
            raise HTTPException(409, "event_cursor_ahead")
        return StreamingResponse(
            (_sse(event) for event in records if event.sequence > cursor),
            media_type="text/event-stream",
            headers=_sse_headers(),
        )

    return router
