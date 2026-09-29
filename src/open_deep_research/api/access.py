"""Native SQL ownership and sandbox authority with owner-scoped archive reads."""

from fastapi import HTTPException

from open_deep_research.api.history import HistoricalRunReader
from open_deep_research.configuration import Configuration
from open_deep_research.agentscope_runtime.native_security import cleanup_run_key, sandbox_context


class RunAccess:
    def __init__(self, native_service):
        self.native_service = native_service

    def require_history_owner(self, run_id, principal):
        cfg = Configuration.from_runnable_config(None)
        try:
            archive = HistoricalRunReader(cfg.runs_dir, run_id, principal.user_id)
        except (OSError, ValueError):
            raise HTTPException(404, "Run not found") from None
        return None, Configuration.from_runnable_config(archive.manifest.config)

    async def run_owner(self, _db, principal, run_id):
        service = self.native_service()
        if service is not None:
            try:
                await service.store.load(run_id, principal.user_id)
                return True
            except KeyError:
                pass
        try:
            self.require_history_owner(run_id, principal)
            return True
        except HTTPException:
            return False

    async def task_owner(self, db, principal, key):
        run_id, task_id = key
        if not await self.run_owner(db, principal, run_id):
            return False
        service = self.native_service()
        if service is not None:
            snapshot = await service.snapshot(run_id, principal.user_id)
            return task_id in snapshot.get("progress", {}).get("task_items", {})
        return False

    async def key_cleanup(self, run_id, manager):
        service = self.native_service()
        return await cleanup_run_key(service.store, run_id, manager) if service is not None else False

    async def sandbox_ledger(self, run_id):
        service = self.native_service()
        return await service.pipeline_factory.gateway_ledger(run_id) if service is not None else None

    async def sandbox_context(self, run_id, *, require_live_fence=False):
        service = self.native_service()
        if service is None:
            raise HTTPException(503, "runtime_unavailable")
        context = await sandbox_context(service, run_id, require_live_fence=require_live_fence)
        if context is None:
            # Archived checkpoints must never grant a new write or approval.
            raise HTTPException(409, "legacy_checkpoint_read_only")
        return context
