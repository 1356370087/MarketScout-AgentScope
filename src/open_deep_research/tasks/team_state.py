"""TaskStateStore implementation backed by transactional team events."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from open_deep_research.tasks.registry import TaskStatus
from open_deep_research.tasks.state import TaskSnapshot, TaskStateStore
from open_deep_research.tasks.team_protocol import TeamEvent
from open_deep_research.tasks.team_service import TeamService


class PostgresTaskStateStore(TaskStateStore):
    """Commit task progress and its notification in one local transaction."""

    def __init__(self, service: TeamService):
        """Reuse the team's pool and transaction producer."""
        self.service = service

    async def get(self, task_id: str, *, run_id: str | None = None) -> TaskSnapshot | None:
        """Read the authoritative snapshot with mandatory run isolation."""
        if run_id is None:
            raise ValueError("run_id_required")
        snapshots = await self.list(run_id=run_id)
        return next((item for item in snapshots if item.task_id == task_id), None)

    async def list(
        self, *, status_filter: TaskStatus | None = None, run_id: str | None = None,
    ) -> list[TaskSnapshot]:
        """Read current task ownership and execution status."""
        if run_id is None:
            raise ValueError("run_id_required")
        rows = await self.service.tasks(run_id)
        result = [TaskSnapshot.model_validate(dict(row, assigned_teammate_id=row["owner"])) for row in rows]
        return [item for item in result if status_filter is None or item.status == status_filter]

    async def upsert(self, snapshot: TaskSnapshot) -> TaskSnapshot:
        """Reject stale versions instead of advancing stale snapshot contents."""
        current = await self.get(snapshot.task_id, run_id=snapshot.run_id)
        if current is not None and current.status in {
            TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.TIMED_OUT,
        } and current.status != snapshot.status:
            # A late Worker checkpoint after TaskStop is a no-op. Do not reserve
            # the next version's operation ID for progress that cannot commit.
            return current
        event = TeamEvent(
            operation_id=f"snapshot:{snapshot.task_id}:{snapshot.version}",
            run_id=snapshot.run_id, sender=snapshot.assigned_teammate_id or "lead",
            recipients=["lead"], type=f"task_{snapshot.status.value}",
            payload={"task_id": snapshot.task_id, "snapshot_version": snapshot.version,
                     "snapshot_digest": hashlib.sha256(snapshot.model_dump_json().encode()).hexdigest()},
            fence_token=snapshot.fence_token,
        )
        async with self.service.store.pool.acquire() as db:
            members = await db.fetch(
                "SELECT member_id FROM research_team_members WHERE run_id=$1 AND status<>'closed'",
                snapshot.run_id,
            )
        event.recipients = sorted({"lead", *(row["member_id"] for row in members)})
        previous = await self.service.store.prepared_event(snapshot.run_id, event.operation_id)
        if previous is not None:
            event.recipients = previous.recipients

        async def mutate(db) -> dict[str, Any]:
            epoch = await db.fetchval(
                "SELECT fence_token FROM research_teams WHERE run_id=$1 FOR SHARE", snapshot.run_id,
            )
            if epoch != snapshot.fence_token:
                raise RuntimeError("stale_team_fence")
            current = await db.fetchrow(
                """SELECT status,version,fence_token,snapshot FROM research_team_tasks
                   WHERE run_id=$1 AND task_id=$2 FOR UPDATE""", snapshot.run_id, snapshot.task_id,
            )
            if current is None:
                raise ValueError("task_must_be_created_before_execution")
            if snapshot.fence_token < current["fence_token"]:
                raise RuntimeError("stale_fence_token")
            if current["status"] in {"completed", "failed", "cancelled", "timed_out"} and current["status"] != snapshot.status.value:
                return json.loads(current["snapshot"])
            if snapshot.version <= current["version"]:
                raise RuntimeError("stale_task_version")
            await db.execute(
                """UPDATE research_team_tasks SET snapshot=$3::jsonb,status=$4,
                   admission_status=$5,version=$6,fence_token=$7,owner=$8
                   WHERE run_id=$1 AND task_id=$2""",
                snapshot.run_id, snapshot.task_id, snapshot.model_dump_json(), snapshot.status.value,
                snapshot.admission_status, snapshot.version, snapshot.fence_token,
                snapshot.assigned_teammate_id,
            )
            return snapshot.model_dump(mode="json")

        result = await self.service.transport.transact(event, mutate)
        return TaskSnapshot.model_validate(result)


class LazyTeamStateStore(TaskStateStore):
    """Resolve deployment clients on first async operation."""

    async def upsert(self, snapshot):
        """Persist through the live transaction producer."""
        from open_deep_research.tasks.team_runtime import team_runtime
        await team_runtime.start()
        return await team_runtime.state.upsert(snapshot)

    async def get(self, task_id, *, run_id=None):
        """Read one run-scoped task."""
        from open_deep_research.tasks.team_runtime import team_runtime
        await team_runtime.start()
        return await team_runtime.state.get(task_id, run_id=run_id)

    async def list(self, *, status_filter=None, run_id=None):
        """Read the persisted shared task list."""
        from open_deep_research.tasks.team_runtime import team_runtime
        await team_runtime.start()
        return await team_runtime.state.list(status_filter=status_filter, run_id=run_id)
