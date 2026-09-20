"""Delete terminal native runs under their SQL fence; leave legacy archives read-only."""

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException
from sqlalchemy import delete, func, or_, select, text

from open_deep_research.agentscope_runtime.recovery_store import FenceLost
from open_deep_research.configuration import Configuration
from open_deep_research.observability.tracing import SQLiteTraceStore
from open_deep_research.report.publication_store import PublicationJobStore

TERMINAL = frozenset({"completed", "failed", "cancelled"})
logger = logging.getLogger(__name__)


def directory_bytes(root):
    """Count regular files without following directory symlinks."""
    import os

    total = 0
    for directory, _, files in os.walk(root, followlinks=False):
        for name in files:
            path = Path(directory) / name
            try:
                if not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                continue
    return total


class NativeRunRetention:
    """Keep ownership recoverable until external cleanup and SQL deletion succeed."""

    def __init__(self, service):
        self.service = service
        self.store = service.store

    def directory(self, run_id):
        if self.service.runs_dir is None:
            raise HTTPException(503, "run_storage_unavailable")
        root = self.service.runs_dir.resolve()
        path = root / run_id
        if path.is_symlink() or path.resolve().parent != root:
            raise ValueError("run_id escapes runs_dir")
        return path

    async def delete(self, run_id, principal, *, force=False, dry_run=False):
        owner = principal.user_id
        if "admin" in principal.roles:
            async with self.store.engine.connect() as conn:
                owner = await conn.scalar(select(self.store.runs.c.user_id).where(self.store.runs.c.run_id == run_id))
            if owner is None:
                raise KeyError("run not found")
        try:
            state, _ = await self.store.load(run_id, owner)
        except KeyError:
            self.service.history(run_id, owner)
            raise HTTPException(409, "legacy_checkpoint_read_only") from None
        cfg = Configuration.from_runnable_config(None)
        path = self.directory(run_id)
        if state.status not in TERMINAL and not force:
            raise HTTPException(409, "run_is_active")
        if dry_run:
            return {"run_id": run_id, "status": "would_delete", "run_status": state.status,
                    "force": force, "directory": str(path), "trace_store": cfg.trace_store_path}
        if state.status not in TERMINAL:
            await self.service.cancel(run_id, owner)
        return await self.purge(run_id, owner, cfg, reason="manual")

    async def _release_external(self, run_id, path):
        # Ciphertext must survive a failed remote key revocation so it can retry.
        if (path / "secrets/litellm-run-key.enc").exists():
            from open_deep_research.models.credentials import (
                LiteLLMKeyAdminClient, RunKeyManager, RunKeySecretStore, RunKeySettings,
            )
            settings = RunKeySettings.from_env()
            manager = RunKeyManager(settings, RunKeySecretStore(str(self.service.runs_dir), settings.encryption_key),
                                    LiteLLMKeyAdminClient(settings))
            try:
                if not await manager.finalize(run_id):
                    raise HTTPException(409, "run_key_cleanup_pending")
            finally:
                await manager.aclose()
        from open_deep_research.documents.repository import release_run_sources
        from open_deep_research.documents.settings import get_document_settings

        if get_document_settings().configured:
            await release_run_sources(run_id)

    async def _delete_team(self, conn, row):
        run_id, owner = row["run_id"], row["user_id"]
        runtime = getattr(self.service.pipeline_factory, "runtime", None)
        if runtime is None:
            return
        members = (await conn.execute(text("SELECT member_id FROM research_team_members WHERE run_id=:run"),
                                      {"run": run_id})).scalars().all()

        def identity(*parts):
            return uuid5(NAMESPACE_URL, json.dumps([owner, run_id, *parts])).hex

        team = await runtime.storage.get_team(owner, identity("team"))
        await runtime.storage.delete_team(owner, identity("team"))
        for member in members:
            if member != "lead":
                await runtime.storage.delete_agent(owner, identity("member", member))
        await runtime.storage.delete_agent(owner, team.leader_agent_id if team else identity("leader-agent"))
        for table in ("research_coordination_receipts", "research_coordination_outbox"):
            await conn.execute(text(f"DELETE FROM {table} WHERE event_id IN "
                                    "(SELECT event_id FROM research_coordination_events WHERE run_id=:run)"), {"run": run_id})
        for table in ("research_coordination_events", "research_coordination_transactions",
                      "research_team_plans", "research_team_proposals", "research_team_dependencies",
                      "research_team_tasks", "research_team_members", "research_teams"):
            await conn.execute(text(f"DELETE FROM {table} WHERE run_id=:run"), {"run": run_id})

    async def purge(self, run_id, owner, cfg, *, reason):
        path = self.directory(run_id)
        lease = await self.store.acquire(run_id, owner)
        try:
            # This existing conditional UPDATE fences both expiry races and
            # concurrent publication creation until deletion commits.
            async with self.store.transaction(lease) as (conn, row):
                if row["snapshot"]["status"] not in TERMINAL:
                    raise HTTPException(409, "run_is_active")
                jobs = await asyncio.to_thread(PublicationJobStore(run_id, runs_dir=self.service.runs_dir).list)
                if any(job.status in {"queued", "running"} for job in jobs):
                    raise HTTPException(409, "publication_in_progress")
                await self._release_external(run_id, path)
                await self._delete_team(conn, row)
                trace_rows = 0
                if Path(cfg.trace_store_path).exists():
                    trace_rows = await asyncio.to_thread(SQLiteTraceStore(cfg.trace_store_path).delete_run, run_id)
                existed = path.exists()
                if existed:
                    await asyncio.to_thread(shutil.rmtree, path)
                for table in (self.store.ops, self.store.outbox, self.store.decisions, self.store.cursors, self.store.runs):
                    await conn.execute(delete(table).where(table.c.run_id == run_id))
            return {"run_id": run_id, "status": "deleted", "reason": reason,
                    "directory_deleted": existed, "trace_rows_deleted": trace_rows}
        finally:
            await self.store.release(lease)

    async def candidates(self):
        runs, events = self.store.runs, self.store.outbox
        terminal_event = or_(
            events.c.payload["type"].as_string() == "research.cancelled",
            (events.c.payload["type"].as_string() == "research.state") & events.c.payload["status"].as_string().in_(TERMINAL),
        )
        ended_at = select(func.max(events.c.payload["timestamp"].as_float())).where(
            events.c.run_id == runs.c.run_id, terminal_event,
        ).scalar_subquery()
        async with self.store.engine.connect() as conn:
            rows = (await conn.execute(select(runs.c.run_id, runs.c.user_id, ended_at.label("ended_at"))
                                       .where(runs.c.snapshot["status"].as_string().in_(TERMINAL)))).mappings().all()
        # Old imported snapshots with no terminal event have no proven age.
        return sorted(rows, key=lambda row: (row["ended_at"] if row["ended_at"] is not None else float("inf"), row["run_id"]))

    async def sweep(self, cfg):
        candidates = await self.candidates()
        deleted = {"retention": 0, "quota": 0}
        removed = set()

        async def remove(row, reason):
            try:
                await self.purge(row["run_id"], row["user_id"], cfg, reason=reason)
            except (FenceLost, HTTPException):
                return
            except Exception as error:
                logger.warning("Native retention cleanup failed: %s", type(error).__name__)
                return
            removed.add(row["run_id"])
            deleted[reason] += 1

        cutoff = time.time() - cfg.run_retention_days * 86400
        if cfg.run_retention_days > 0:
            for row in candidates:
                if row["ended_at"] is not None and row["ended_at"] < cutoff:
                    await remove(row, "retention")
        used = await asyncio.to_thread(directory_bytes, self.service.runs_dir)
        quota = cfg.runs_dir_max_bytes
        quota_triggered = quota > 0 and used > quota
        if quota_triggered:
            for row in candidates:
                if used <= int(quota * 0.9):
                    break
                if row["run_id"] not in removed:
                    await remove(row, "quota")
                    used = await asyncio.to_thread(directory_bytes, self.service.runs_dir)
        trace_deleted = 0
        trace_days = cfg.run_retention_days if cfg.trace_retention_days is None else cfg.trace_retention_days
        if trace_days > 0 and Path(cfg.trace_store_path).exists():
            traces = SQLiteTraceStore(cfg.trace_store_path)
            for row in candidates:
                if row["ended_at"] is not None and row["ended_at"] < time.time() - trace_days * 86400:
                    trace_deleted += int(bool(await asyncio.to_thread(traces.delete_run, row["run_id"])))
            await asyncio.to_thread(traces.checkpoint)
        return {"status": "completed", "deleted_by_age": deleted["retention"], "deleted_by_quota": deleted["quota"],
                "trace_runs_deleted": trace_deleted, "used_bytes": used,
                "quota_exceeded": quota_triggered and used > int(quota * 0.9)}

    async def loop(self, cfg):
        while not self.service.closed:
            await asyncio.sleep(cfg.retention_sweep_interval_seconds)
            try:
                await self.sweep(cfg)
            except Exception as error:
                logger.warning("Native retention sweep failed: %s", type(error).__name__)
