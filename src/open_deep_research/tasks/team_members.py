"""Persistent member sessions, distinct from per-task research state."""

from __future__ import annotations

import json
from typing import Any

from open_deep_research.tasks.team_protocol import TeamEvent
from open_deep_research.tasks.team_store import TeamStore


class MemberSessions:
    """Commit member input and its application marker in one DB transaction."""

    def __init__(self, store: TeamStore):
        """Reuse the deployment's coordination store."""
        self.store = store

    async def accept(self, run_id: str, member_id: str, event: TeamEvent) -> None:
        """Append received conversation input exactly once across restarts."""
        async with self.store.pool.acquire() as db, db.transaction():
            applied = await db.fetchval(
                """UPDATE research_coordination_receipts r SET applied=true
                   FROM research_coordination_events e WHERE r.event_id=e.event_id
                   AND e.run_id=$1 AND r.event_id=$2 AND r.recipient=$3 AND NOT r.applied
                   RETURNING r.event_id""", run_id, event.event_id, member_id,
            )
            if applied is None:
                return
            if event.type != "message":
                return
            message = {"event_id": event.event_id, "sender": event.sender,
                       "type": event.type, "payload": event.payload}
            updated = await db.fetchval(
                """UPDATE research_team_members SET session=jsonb_set(
                     session,'{messages}',COALESCE(session->'messages','[]'::jsonb)||$3::jsonb),
                     version=version+1
                   WHERE run_id=$1 AND member_id=$2 RETURNING member_id""",
                run_id, member_id, json.dumps([message]),
            )
            if updated is None:
                raise ValueError("unknown_team_member")

    async def load(self, run_id: str, member_id: str) -> dict[str, Any]:
        """Restore the member conversation without inheriting task evidence."""
        async with self.store.pool.acquire() as db:
            value = await db.fetchval(
                "SELECT session FROM research_team_members WHERE run_id=$1 AND member_id=$2",
                run_id, member_id,
            )
        if value is None:
            raise ValueError("unknown_team_member")
        return json.loads(value)

    async def remember_result(self, run_id: str, member_id: str, task_id: str, summary: str, artifact: str) -> None:
        """Retain bounded findings and an explicit artifact reference per task."""
        result = {"task_id": task_id, "summary": summary[:12000], "artifact": artifact}
        async with self.store.pool.acquire() as db:
            await db.execute(
                """UPDATE research_team_members SET session=jsonb_set(
                    session,'{results}',COALESCE(session->'results','{}'::jsonb)||$3::jsonb),
                    version=version+1 WHERE run_id=$1 AND member_id=$2""",
                run_id, member_id, json.dumps({task_id: result}),
            )
