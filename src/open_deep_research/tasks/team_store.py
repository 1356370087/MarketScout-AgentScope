"""PostgreSQL transaction outcomes and durable coordination receipts."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg

from open_deep_research.tasks.team_protocol import TeamEvent


class TeamStore:
    """One process-owned pool; short transactions contain no model calls."""

    def __init__(self, pool: asyncpg.Pool):
        """Use an externally managed pool so shutdown is explicit."""
        self.pool = pool

    async def prepared_event(self, run_id: str, operation_id: str) -> TeamEvent | None:
        """Recover the original recipient set across a retried broadcast."""
        async with self.pool.acquire() as db:
            value = await db.fetchval(
                "SELECT event FROM research_coordination_transactions WHERE run_id=$1 AND operation_id=$2",
                run_id, operation_id,
            )
        return TeamEvent.model_validate_json(value) if value else None

    async def prepare(self, event: TeamEvent) -> tuple[str, Any]:
        """Reserve a stable operation and reject reuse with different input."""
        async with self.pool.acquire() as db:
            await db.execute(
                """INSERT INTO research_coordination_transactions
                   (run_id, operation_id, event_id, state, event)
                   VALUES ($1,$2,$3,'PREPARED',$4::jsonb)
                   ON CONFLICT (run_id, operation_id) DO NOTHING""",
                event.run_id, event.operation_id, event.event_id, event.model_dump_json(),
            )
            row = await db.fetchrow(
                """SELECT state,event,result FROM research_coordination_transactions
                   WHERE run_id=$1 AND operation_id=$2""", event.run_id, event.operation_id,
            )
        original = TeamEvent.model_validate_json(row["event"])
        excluded = {"event_id", "fence_token", "trace_context", "execution_epoch"}
        if original.model_dump(exclude=excluded) != event.model_dump(exclude=excluded):
            raise ValueError("coordination_operation_input_mismatch")
        event.event_id = original.event_id
        return row["state"], json.loads(row["result"]) if row["result"] else None

    async def commit(
        self, event: TeamEvent,
        mutate: Callable[[asyncpg.Connection], Awaitable[Any]],
        *, durable_recipients: bool = False, outbox: bool = False,
    ) -> Any:
        """Commit mutation and its transaction outcome atomically."""
        async with self.pool.acquire() as db, db.transaction():
            claimed = await db.fetchval(
                """UPDATE research_coordination_transactions SET state='COMMITTED'
                   WHERE run_id=$1 AND operation_id=$2 AND state='PREPARED'
                     AND deadline > clock_timestamp() RETURNING event_id""",
                event.run_id, event.operation_id,
            )
            if claimed is None:
                row = await db.fetchrow(
                    """SELECT state,result FROM research_coordination_transactions
                       WHERE run_id=$1 AND operation_id=$2""", event.run_id, event.operation_id,
                )
                if row and row["state"] == "COMMITTED":
                    return json.loads(row["result"])
                raise RuntimeError("coordination_transaction_expired")
            result = await mutate(db)
            await db.execute(
                """UPDATE research_coordination_transactions SET result=$3::jsonb
                   WHERE run_id=$1 AND operation_id=$2""",
                event.run_id, event.operation_id, json.dumps(result),
            )
            await db.execute(
                """INSERT INTO research_coordination_events(event_id,run_id,event)
                   VALUES ($1,$2,$3::jsonb) ON CONFLICT(event_id) DO NOTHING""",
                event.event_id, event.run_id, event.model_dump_json(),
            )
            if durable_recipients and event.recipients:
                await db.executemany(
                    """INSERT INTO research_coordination_receipts(event_id,recipient)
                       VALUES ($1,$2) ON CONFLICT DO NOTHING""",
                    [(event.event_id, recipient) for recipient in event.recipients],
                )
            if outbox:
                await db.execute("INSERT INTO research_coordination_outbox(event_id) VALUES($1) ON CONFLICT DO NOTHING", event.event_id)
            return result

    async def abort(self, event: TeamEvent) -> None:
        """Never roll back an operation whose database commit already succeeded."""
        async with self.pool.acquire() as db:
            await db.execute(
                """UPDATE research_coordination_transactions SET state='ABORTED'
                   WHERE event_id=$1 AND state='PREPARED'""", event.event_id,
            )

    async def outcome(self, event_id: str) -> str:
        """Resolve broker checks without guessing on an unavailable database."""
        async with self.pool.acquire() as db:
            await db.execute(
                """UPDATE research_coordination_transactions SET state='ABORTED'
                   WHERE event_id=$1 AND state='PREPARED' AND deadline <= clock_timestamp()""",
                event_id,
            )
            return await db.fetchval(
                "SELECT state FROM research_coordination_transactions WHERE event_id=$1",
                event_id,
            ) or "ABORTED"

    async def receive(self, event: TeamEvent) -> None:
        """Durably accept every recipient before acknowledging the broker."""
        async with self.pool.acquire() as db, db.transaction():
            await db.execute(
                """INSERT INTO research_coordination_events(event_id,run_id,event)
                   VALUES ($1,$2,$3::jsonb) ON CONFLICT(event_id) DO NOTHING""",
                event.event_id, event.run_id, event.model_dump_json(),
            )
            await db.executemany(
                """INSERT INTO research_coordination_receipts(event_id,recipient)
                   VALUES ($1,$2) ON CONFLICT DO NOTHING""",
                [(event.event_id, recipient) for recipient in event.recipients],
            )

    async def pending(self, run_id: str, recipient: str) -> list[TeamEvent]:
        """Read business input still awaiting a durable agent checkpoint."""
        async with self.pool.acquire() as db:
            rows = await db.fetch(
                """SELECT e.event FROM research_coordination_events e
                   JOIN research_coordination_receipts r USING(event_id)
                   WHERE e.run_id=$1 AND r.recipient=$2 AND NOT r.applied
                   ORDER BY CASE WHEN e.event->'payload'->'message'->>'type' IN
                     ('plan_approval_response','shutdown_request','shutdown_response') OR e.event->>'type' IN
                     ('cancel_request','task_stop','shutdown_request','shutdown_response','shutdown_ack')
                     THEN 0 ELSE 1 END, e.sequence LIMIT 100""", run_id, recipient,
            )
        return [TeamEvent.model_validate_json(row["event"]) for row in rows]

    async def applied(self, run_id: str, recipient: str, event_ids: list[str]) -> None:
        """Mark input only after the caller commits its state or journal."""
        async with self.pool.acquire() as db:
            await db.execute(
                """UPDATE research_coordination_receipts r SET applied=true
                   FROM research_coordination_events e WHERE e.event_id=r.event_id
                   AND e.run_id=$1 AND r.recipient=$2 AND r.event_id=ANY($3::text[])""",
                run_id, recipient, event_ids,
            )
