"""Transactional M6 run checkpoints, operation receipts, decisions and outbox.

PostgreSQL uses the runtime schema; demo mode uses a separate SQLite database.
The run row owns the business lease. Native MessageBus session locks continue
to own session scheduling, not business result commits.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    delete,
    insert,
    literal_column,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import create_async_engine

from open_deep_research.agentscope_runtime.research_pipeline import ResearchSnapshot
from open_deep_research.budgets import (
    BudgetDimension,
    BudgetExhausted,
    DeadlineExceeded,
)


class FenceLost(RuntimeError):
    """An expired executor must not mutate the run."""


class UnknownOperation(RuntimeError):
    """An external effect needs reconciliation, never automatic repetition."""


class RecoveryConflict(ValueError):
    """An idempotency key or revision was reused with different content."""


@dataclass(frozen=True)
class RunLease:
    run_id: str
    user_id: str
    owner: str
    fence: int


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _no_credentials(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {
                "api_key",
                "apikey",
                "apikeys",
                "access_token",
                "refresh_token",
                "authorization",
                "secret_key",
            }:
                raise ValueError(
                    "checkpoint contains a credential field; use a reference"
                )
            _no_credentials(item)
    elif isinstance(value, list):
        for item in value:
            _no_credentials(item)


class RecoveryStore:
    """Every business write checks the same run fence inside its transaction."""

    def __init__(self, url, *, engine_kwargs=None):
        self.engine = create_async_engine(url, **(engine_kwargs or {}))
        self.commit_guard = None
        self.meta = MetaData()
        self.runs = Table(
            "as_recovery_runs",
            self.meta,
            Column("run_id", String, primary_key=True),
            Column("user_id", String, nullable=False),
            Column("version", Integer, nullable=False),
            Column("engine", String, nullable=False),
            Column("revision", Integer, nullable=False),
            Column("snapshot", JSON, nullable=False),
            Column("owner", String),
            Column("fence", Integer, nullable=False),
            Column("expires", Float, nullable=False),
            Column("limits", JSON, nullable=False),
            Column("used", JSON, nullable=False),
            Column("reserved", JSON, nullable=False),
            Column("deadline", Float),
            Column("task_ids", JSON, nullable=False),
        )
        self.ops = Table(
            "as_recovery_operations",
            self.meta,
            Column("run_id", String, primary_key=True),
            Column("key", String, primary_key=True),
            Column("input_digest", String, nullable=False),
            Column("kind", String, nullable=False),
            Column("state", String, nullable=False),
            Column("replay_safe", Integer, nullable=False),
            Column("fence", Integer, nullable=False),
            Column("result", JSON),
            Column("reservation", JSON, nullable=False),
            Column("actual", JSON),
        )
        self.outbox = Table(
            "as_recovery_outbox",
            self.meta,
            Column("run_id", String, primary_key=True),
            Column("event_id", String, primary_key=True),
            Column("sequence", Integer, nullable=False),
            Column("payload", JSON, nullable=False),
        )
        self.decisions = Table(
            "as_recovery_decisions",
            self.meta,
            Column("run_id", String, primary_key=True),
            Column("command_id", String, primary_key=True),
            Column("action_id", String, nullable=False),
            Column("payload", JSON, nullable=False),
            Column("state", String, nullable=False),
        )
        self.cursors = Table(
            "as_recovery_cursors",
            self.meta,
            Column("run_id", String, primary_key=True),
            Column("consumer", String, primary_key=True),
            Column("sequence", Integer, nullable=False),
        )

    async def create_tables(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(self.meta.create_all)

    async def aclose(self):
        await self.engine.dispose()

    async def _now(self, conn):
        sql = (
            "SELECT EXTRACT(EPOCH FROM clock_timestamp())"
            if conn.dialect.name == "postgresql"
            else "SELECT (julianday('now') - 2440587.5) * 86400.0"
        )
        return float(await conn.scalar(text(sql)))

    @staticmethod
    def _clock(conn):
        return literal_column(
            "EXTRACT(EPOCH FROM clock_timestamp())"
            if conn.dialect.name == "postgresql"
            else "(julianday('now') - 2440587.5) * 86400.0"
        )

    async def create_run(
        self, user_id, snapshot: ResearchSnapshot, *, limits=None, deadline=None
    ):
        body = snapshot.model_dump(mode="json")
        _no_credentials(body)
        known = {dimension.value for dimension in BudgetDimension}
        if set(limits or {}) - known or any(v < 0 for v in (limits or {}).values()):
            raise ValueError("invalid six-dimensional budget limits")
        async with self.engine.begin() as conn:
            await conn.execute(
                insert(self.runs).values(
                    run_id=snapshot.run_id,
                    user_id=user_id,
                    version=1,
                    engine="agentscope",
                    revision=0,
                    snapshot=body,
                    owner=None,
                    fence=0,
                    expires=0,
                    limits=limits or {},
                    used={},
                    reserved={},
                    deadline=deadline,
                    task_ids=[],
                )
            )

    async def create_from_config(self, user_id, run_id, run_config, *, messages=(), application=None):
        """Reuse project budget configuration while keeping one SQL authority."""
        import time

        from open_deep_research.budgets import budget_policy_from_config
        from open_deep_research.configuration import Configuration

        cfg = Configuration(
            **{name: run_config.get(name) for name in Configuration.model_fields}
        )
        policy = budget_policy_from_config(cfg)
        limits = {
            item.value: policy.limit_for(item)
            for item in BudgetDimension
            if policy and policy.limit_for(item) is not None
        }
        state = ResearchSnapshot(
            run_id=run_id,
            config_fingerprint=run_config.compatibility_projection()["metadata"][
                "run_config_fingerprint"
            ],
            messages=list(messages),
            application=application or {},
        )
        await self.create_run(
            user_id,
            state,
            limits=limits,
            deadline=time.time() + cfg.run_deadline_seconds
            if cfg.run_deadline_seconds
            else None,
        )
        return state

    async def load(self, run_id, user_id):
        async with self.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(self.runs).where(
                            self.runs.c.run_id == run_id, self.runs.c.user_id == user_id
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise KeyError("run not found")
        if row["version"] != 1 or row["engine"] != "agentscope":
            raise ValueError("unsupported checkpoint version or engine")
        body = row["snapshot"]
        if set(body) - set(ResearchSnapshot.model_fields):
            raise ValueError("checkpoint has unknown version fields")
        return ResearchSnapshot.model_validate(body), row["revision"]

    async def acquire(self, run_id, user_id, *, owner=None, ttl=30):
        if ttl <= 0:
            raise ValueError("lease TTL must be positive")
        owner = owner or uuid4().hex
        async with self.engine.begin() as conn:
            now = self._clock(conn)
            row = (
                await conn.execute(
                    update(self.runs)
                    .where(
                        self.runs.c.run_id == run_id,
                        self.runs.c.user_id == user_id,
                        self.runs.c.expires <= now,
                    )
                    .values(owner=owner, fence=self.runs.c.fence + 1, expires=now + ttl)
                    .returning(self.runs.c.fence)
                )
            ).first()
        if row is None:
            raise FenceLost("run already owned or not visible")
        return RunLease(run_id, user_id, owner, row[0])

    def _identity(self, lease):
        return and_(
            self.runs.c.run_id == lease.run_id,
            self.runs.c.user_id == lease.user_id,
            self.runs.c.owner == lease.owner,
            self.runs.c.fence == lease.fence,
            self.runs.c.version == 1,
            self.runs.c.engine == "agentscope",
        )

    @asynccontextmanager
    async def transaction(self, lease):
        async with self.engine.begin() as conn:
            now = self._clock(conn)
            row = (
                (
                    await conn.execute(
                        update(self.runs)
                        .where(self._identity(lease), self.runs.c.expires > now)
                        .values(revision=self.runs.c.revision + 1)
                        .returning(*self.runs.c)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise FenceLost("expired or superseded run fence")
            if self.commit_guard is not None:
                await self.commit_guard(conn)
            yield conn, dict(row)

    async def renew(self, lease, ttl=30):
        async with self.transaction(lease) as (conn, _row):
            await conn.execute(
                update(self.runs)
                .where(self._identity(lease))
                .values(expires=await self._now(conn) + ttl)
            )

    async def release(self, lease):
        async with self.engine.begin() as conn:
            await conn.execute(
                update(self.runs)
                .where(self._identity(lease))
                .values(expires=0, owner=None)
            )

    async def _event(self, conn, row, kind, payload, event_id=None):
        event_id = event_id or uuid4().hex
        await conn.execute(
            insert(self.outbox).values(
                run_id=row["run_id"],
                event_id=event_id,
                sequence=row["revision"],
                payload={"event_id": event_id, "type": kind, "timestamp": await self._now(conn), **payload},
            )
        )

    async def save(
        self, lease, snapshot, *, expected_revision=None, command_id=None, limits=None
    ):
        body = snapshot.model_dump(mode="json")
        _no_credentials(body)
        if snapshot.run_id != lease.run_id:
            raise RecoveryConflict("checkpoint run mismatch")
        async with self.transaction(lease) as (conn, row):
            if body["config_fingerprint"] != row["snapshot"]["config_fingerprint"]:
                raise RecoveryConflict("frozen configuration changed")
            if (
                expected_revision is not None
                and row["revision"] != expected_revision + 1
            ):
                raise RecoveryConflict("checkpoint revision changed")
            await conn.execute(
                update(self.runs).where(self._identity(lease)).values(snapshot=body)
            )
            configuration = body.get("application", {}).get("configuration", {}).get("contract", {}).get("configurable", {})
            if snapshot.status == "failed" and self.engine.dialect.name == "postgresql" and configuration.get("async_research_mode") == "teams":
                # A failed Lead cannot leave tasks or approvals apparently active.
                params = {"run": lease.run_id}
                await conn.execute(text("""UPDATE research_team_tasks SET status='failed',phase=NULL,version=version+1
                    WHERE run_id=:run AND status IN ('pending','running','waiting_for_confirmation')"""), params)
                await conn.execute(text("UPDATE research_team_plans SET status='superseded' WHERE run_id=:run AND status='pending'"), params)
                await conn.execute(text("UPDATE research_team_proposals SET status='cancelled' WHERE run_id=:run AND status='pending'"), params)
                await conn.execute(text("UPDATE research_team_members SET status='failed',execution_token=NULL,lease_expires=NULL WHERE run_id=:run AND status<>'closed'"), params)
                await conn.execute(text("UPDATE research_teams SET status='failed' WHERE run_id=:run"), params)
            if limits is not None:
                for dimension, maximum in limits.items():
                    BudgetDimension(dimension)
                    if not isinstance(maximum, int) or maximum < row["used"].get(
                        dimension, 0
                    ) + row["reserved"].get(dimension, 0):
                        raise ValueError(
                            "approved limit cannot be below committed and reserved usage"
                        )
                await conn.execute(
                    update(self.runs)
                    .where(self._identity(lease))
                    .values(limits={**row["limits"], **limits})
                )
            decision = None
            if command_id:
                updated = await conn.execute(
                    update(self.decisions)
                    .where(
                        self.decisions.c.run_id == lease.run_id,
                        self.decisions.c.command_id == command_id,
                        self.decisions.c.state == "pending",
                    )
                    .values(state="applied")
                )
                if updated.rowcount != 1:
                    raise RecoveryConflict("decision already applied or missing")
                record = (
                    (
                        await conn.execute(
                            select(self.decisions).where(
                                self.decisions.c.run_id == lease.run_id,
                                self.decisions.c.command_id == command_id,
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                old_approval = (
                    row["snapshot"].get("approvals", {}).get(record["action_id"], {})
                )
                decision = {
                    "action_id": record["action_id"],
                    "action": record["payload"]["action"],
                    "kind": old_approval.get("kind")
                    or (row["snapshot"].get("pending") or {}).get("stage"),
                }
            await self._event(
                conn,
                row,
                "research.state",
                {
                    "status": snapshot.status,
                    "stage": snapshot.inflight,
                    "completed": snapshot.completed,
                    "command_id": command_id,
                    "pending": snapshot.pending.model_dump(mode="json")
                    if snapshot.pending
                    else None,
                    "result_status": snapshot.completion_outcome.get("action"),
                    "error_code": snapshot.error,
                    "approvals": snapshot.approvals,
                    "decision": decision,
                },
            )
        return row["revision"]

    async def begin_operation(
        self, lease, key, kind, payload, *, replay_safe=False, reserve=None, observation=None
    ):
        fingerprint = digest(payload)
        unknown = False
        async with self.transaction(lease) as (conn, row):
            op = (
                (
                    await conn.execute(
                        select(self.ops).where(
                            self.ops.c.run_id == lease.run_id, self.ops.c.key == key
                        )
                    )
                )
                .mappings()
                .first()
            )
            if op:
                if op["input_digest"] != fingerprint or op["kind"] != kind:
                    raise RecoveryConflict("operation key reused with different input")
                if op["state"] == "committed":
                    return {"replayed": True, "result": op["result"]}
                if (
                    row["deadline"] is not None
                    and await self._now(conn) >= row["deadline"]
                ):
                    raise DeadlineExceeded(
                        "run deadline exceeded before operation retry"
                    )
                if op["state"] == "quarantined" or not op["replay_safe"]:
                    await conn.execute(
                        update(self.ops)
                        .where(self.ops.c.run_id == lease.run_id, self.ops.c.key == key)
                        .values(state="quarantined")
                    )
                    unknown = True
                elif op["fence"] == lease.fence:
                    raise RecoveryConflict("operation already executing in this epoch")
                else:
                    await conn.execute(
                        update(self.ops)
                        .where(self.ops.c.run_id == lease.run_id, self.ops.c.key == key)
                        .values(fence=lease.fence)
                    )
            else:
                if (
                    row["deadline"] is not None
                    and await self._now(conn) >= row["deadline"]
                ):
                    raise DeadlineExceeded("run deadline exceeded")
                reserve = reserve or {}
                reserved = dict(row["reserved"])
                for dimension, amount in reserve.items():
                    BudgetDimension(dimension)
                    if amount < 0:
                        raise ValueError("negative reservation")
                    maximum = row["limits"].get(dimension)
                    if (
                        maximum is not None
                        and row["used"].get(dimension, 0)
                        + reserved.get(dimension, 0)
                        + amount
                        > maximum
                    ):
                        raise BudgetExhausted(BudgetDimension(dimension))
                    reserved[dimension] = reserved.get(dimension, 0) + amount
                await conn.execute(
                    update(self.runs)
                    .where(self._identity(lease))
                    .values(reserved=reserved)
                )
                await conn.execute(
                    insert(self.ops).values(
                        run_id=lease.run_id,
                        key=key,
                        kind=kind,
                        input_digest=fingerprint,
                        state="started",
                        replay_safe=int(replay_safe),
                        fence=lease.fence,
                        result=None,
                        reservation=reserve,
                        actual=None,
                    )
                )
                if observation is not None:
                    # Only content-free dimensions are accepted, never request bodies.
                    dimensions = {k: observation[k] for k in
                                  ("task_id", "agent_role", "stage", "model", "tool_name")
                                  if observation.get(k) is not None}
                    await self._event(conn, row, "research.operation_started",
                                      {"operation_key": key, "kind": kind, **dimensions})
        if unknown:
            raise UnknownOperation(key)
        return {"replayed": False}

    async def commit_operation(self, lease, key, result, *, actual=None):
        _no_credentials(result)
        async with self.transaction(lease) as (conn, row):
            await self._commit_operation(conn, row, lease, key, result, actual=actual)

    async def _commit_operation(self, conn, row, lease, key, result, *, actual=None):
        """Commit within the caller's fenced transaction, including reconciliation."""
        op = (
            (
                await conn.execute(
                    select(self.ops).where(
                        self.ops.c.run_id == lease.run_id, self.ops.c.key == key
                    )
                )
            )
            .mappings()
            .one()
        )
        if op["state"] == "committed":
            original = {k: v for k, v in op["result"].items() if k != "usage_correction"}
            incoming = {k: v for k, v in result.items() if k != "usage_correction"}
            if digest(original) != digest(incoming):
                raise RecoveryConflict("conflicting operation receipt")
            return
        if op["state"] != "started" or op["fence"] != lease.fence:
            raise FenceLost(
                "operation belongs to another epoch or needs reconciliation"
            )
        used, reserved = dict(row["used"]), dict(row["reserved"])
        actual = dict(op["reservation"] if actual is None else actual)
        for dimension, amount in actual.items():
            BudgetDimension(dimension)
            if amount < 0:
                raise ValueError("negative settlement")
            # Keep actual accounting even if a provider violated its declared cap.
            used[dimension] = used.get(dimension, 0) + amount
        for dimension, amount in op["reservation"].items():
            reserved[dimension] = reserved.get(dimension, 0) - amount
        await conn.execute(
            update(self.runs)
            .where(self._identity(lease))
            .values(used=used, reserved=reserved)
        )
        await conn.execute(
            update(self.ops)
            .where(self.ops.c.run_id == lease.run_id, self.ops.c.key == key)
            .values(state="committed", result=result, actual=actual)
        )
        await self._event(
            conn,
            row,
            "research.operation_committed",
            {"operation_key": key, "kind": op["kind"], "usage": actual},
            event_id=digest([lease.run_id, key]),
        )

    async def reconcile_model_usage(self, lease, key, *, receipt_id, input_tokens,
                                    output_tokens, cost_micro_usd=None):
        """Replace estimated usage with verified provider facts, once per receipt.

        The trusted caller must verify the provider receipt belongs to this attempt.
        Keep the execution result immutable so operation replay remains valid.
        """
        facts = {"input_tokens": input_tokens, "output_tokens": output_tokens}
        if cost_micro_usd is not None:
            facts["cost_micro_usd"] = cost_micro_usd
        if not receipt_id or any(type(n) is not int or n < 0 for n in facts.values()):
            raise ValueError("provider receipt requires nonnegative integer usage")
        async with self.transaction(lease) as (conn, row):
            op = (await conn.execute(select(self.ops).where(
                self.ops.c.run_id == lease.run_id, self.ops.c.key == key
            ))).mappings().one()
            if op["kind"] not in {"model_attempt", "gateway:model"} or op["state"] != "committed":
                raise RecoveryConflict("only committed native model attempts can be reconciled")
            result = dict(op["result"])
            correction = {"receipt_id": receipt_id, "usage": facts}
            if result.get("usage_correction"):
                if result["usage_correction"] != correction:
                    raise RecoveryConflict("conflicting provider usage receipt")
                return
            if result.get("usage_status") != "estimated" and result.get("cost_status") == "reported":
                raise RecoveryConflict("model usage already reported")
            if result.get("usage_status") == "reported" and result["observed_usage"] != {
                "input_tokens": input_tokens, "output_tokens": output_tokens,
            }:
                raise RecoveryConflict("provider receipt conflicts with reported tokens")
            if op["kind"] == "gateway:model":
                reported = (result.get("outcome") or {}).get("usage") or {}
                if any(reported.get(k) is not None and reported[k] != facts[k]
                       for k in ("input_tokens", "output_tokens")):
                    raise RecoveryConflict("provider receipt conflicts with reported tokens")
            actual = dict(op["actual"])
            actual.update(facts)
            pricing = result.get("pricing_micro_usd")
            if cost_micro_usd is None and pricing is not None:
                import math
                actual["cost_micro_usd"] = math.ceil(input_tokens * pricing[0] + output_tokens * pricing[1])
            used = dict(row["used"])
            for dimension, amount in actual.items():
                used[dimension] = used.get(dimension, 0) + amount - op["actual"].get(dimension, 0)
            result["usage_correction"] = correction
            await conn.execute(update(self.runs).where(self._identity(lease)).values(used=used))
            await conn.execute(update(self.ops).where(
                self.ops.c.run_id == lease.run_id, self.ops.c.key == key
            ).values(result=result, actual=actual))
            await self._event(conn, row, "research.usage_reconciled",
                              {"operation_key": key, "usage": actual},
                              event_id=digest([lease.run_id, key, "usage_correction"]))

    async def resolve_operation(self, lease, key, *, result=None, not_executed=False):
        """Apply externally verified resolution and settlement in one transaction."""
        if not_executed and result is not None:
            raise ValueError("resolution cannot both report a result and deny execution")
        if not not_executed:
            if not isinstance(result, dict):
                raise ValueError("verified operation result required")
            _no_credentials(result)
        async with self.transaction(lease) as (conn, row):
            op = (await conn.execute(select(self.ops).where(
                self.ops.c.run_id == lease.run_id, self.ops.c.key == key
            ))).mappings().one()
            if op["state"] == "committed":
                if not_executed:
                    raise RecoveryConflict("cannot deny a committed operation")
                await self._commit_operation(conn, row, lease, key, result)
                return
            if not_executed:
                reserved = dict(row["reserved"])
                for dimension, amount in op["reservation"].items():
                    reserved[dimension] -= amount
                await conn.execute(update(self.runs).where(self._identity(lease)).values(reserved=reserved))
                await conn.execute(delete(self.ops).where(
                    self.ops.c.run_id == lease.run_id, self.ops.c.key == key
                ))
            else:
                await conn.execute(update(self.ops).where(
                    self.ops.c.run_id == lease.run_id, self.ops.c.key == key
                ).values(state="started", fence=lease.fence))
                await self._commit_operation(conn, row, lease, key, result)
            await self._event(conn, row, "research.operation_resolved",
                              {"operation_key": key, "not_executed": not_executed})

    async def events(self, run_id, user_id, *, after=0):
        await self.load(run_id, user_id)
        async with self.engine.connect() as conn:
            return [
                dict(row)
                for row in (
                    await conn.execute(
                        select(self.outbox)
                        .where(
                            self.outbox.c.run_id == run_id,
                            self.outbox.c.sequence > after,
                        )
                        .order_by(self.outbox.c.sequence)
                    )
                ).mappings()
            ]

    async def project(self, lease, consumer, apply):
        """Apply projections and advance the cursor in the same database transaction.

        ``apply(conn, event)`` may only write transactional projections on conn.
        External delivery uses the stable event_id and receiver deduplication.
        """
        async with self.transaction(lease) as (conn, _row):
            cursor = await conn.scalar(
                select(self.cursors.c.sequence).where(
                    self.cursors.c.run_id == lease.run_id,
                    self.cursors.c.consumer == consumer,
                )
            )
            events = (
                (
                    await conn.execute(
                        select(self.outbox)
                        .where(
                            self.outbox.c.run_id == lease.run_id,
                            self.outbox.c.sequence > (cursor or 0),
                        )
                        .order_by(self.outbox.c.sequence)
                    )
                )
                .mappings()
                .all()
            )
            for event in events:
                await apply(conn, dict(event))
            if events:
                if cursor is None:
                    await conn.execute(
                        insert(self.cursors).values(
                            run_id=lease.run_id,
                            consumer=consumer,
                            sequence=events[-1]["sequence"],
                        )
                    )
                else:
                    await conn.execute(
                        update(self.cursors)
                        .where(
                            self.cursors.c.run_id == lease.run_id,
                            self.cursors.c.consumer == consumer,
                        )
                        .values(sequence=events[-1]["sequence"])
                    )
            return len(events)

    async def submit_decision(self, run_id, user_id, command_id, action_id, payload):
        """Durably queue an exact action; no wakeup happens before this commits."""
        async with self.engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        update(self.runs)
                        .where(
                            self.runs.c.run_id == run_id, self.runs.c.user_id == user_id
                        )
                        .values(revision=self.runs.c.revision + 1)
                        .returning(*self.runs.c)
                    )
                )
                .mappings()
                .first()
            )
            if not row:
                raise KeyError("run not found")
            previous = (
                (
                    await conn.execute(
                        select(self.decisions).where(
                            self.decisions.c.run_id == run_id,
                            self.decisions.c.command_id == command_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if previous:
                if previous["action_id"] != action_id or previous["payload"] != payload:
                    raise RecoveryConflict(
                        "decision command reused with different content"
                    )
                return previous["state"]
            same_action = (
                (
                    await conn.execute(
                        select(self.decisions).where(
                            self.decisions.c.run_id == run_id,
                            self.decisions.c.action_id == action_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if same_action and payload.get("action") != "feedback":
                if same_action["payload"] != payload:
                    raise RecoveryConflict("approval already has a different decision")
                return same_action["state"]
            pending = row["snapshot"].get("pending")
            approvals = row["snapshot"].get("approvals", {})
            feedback = payload.get("action") == "feedback"
            if feedback:
                if (
                    payload.get("task_id") not in row["task_ids"]
                    or action_id != "feedback:" + payload["task_id"]
                ):
                    raise RecoveryConflict("feedback targets a foreign task")
            elif action_id != (pending or {}).get("id") and action_id not in approvals:
                raise RecoveryConflict("stale or foreign approval")
            if payload.get("action") not in {
                "approve",
                "revise",
                "cancel",
                "answer",
                "feedback",
            }:
                raise ValueError("invalid decision action")
            _no_credentials(payload)
            await conn.execute(
                insert(self.decisions).values(
                    run_id=run_id,
                    command_id=command_id,
                    action_id=action_id,
                    payload=payload,
                    state="pending",
                )
            )
            await self._event(
                conn,
                row,
                "research.decision_queued",
                {"command_id": command_id, "action_id": action_id},
            )
        return "pending"

    async def pending_decisions(self, lease):
        async with self.transaction(lease) as (conn, _row):
            return [
                dict(item)
                for item in (
                    await conn.execute(
                        select(self.decisions).where(
                            self.decisions.c.run_id == lease.run_id,
                            self.decisions.c.state == "pending",
                        )
                    )
                ).mappings()
            ]

    async def budget(self, run_id, user_id):
        await self.load(run_id, user_id)
        async with self.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(
                            self.runs.c.limits,
                            self.runs.c.used,
                            self.runs.c.reserved,
                            self.runs.c.deadline,
                        ).where(self.runs.c.run_id == run_id)
                    )
                )
                .mappings()
                .one()
            )
        return dict(row)

    async def operation_record(self, lease, key):
        """Read a receipt for reconstruction; all subsequent mutations recheck fence."""
        async with self.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(self.ops).where(
                            self.ops.c.run_id == lease.run_id, self.ops.c.key == key
                        )
                    )
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    async def deliver_public(self, lease, publisher, *, consumer="public-events"):
        """Deliver committed outbox events to the existing deduplicating JSONL log."""
        from open_deep_research.agentscope_runtime.recovery_events import public_events

        async with self.engine.connect() as conn:
            cursor = await conn.scalar(
                select(self.cursors.c.sequence).where(
                    self.cursors.c.run_id == lease.run_id,
                    self.cursors.c.consumer == consumer,
                )
            )
        events = await self.events(lease.run_id, lease.user_id, after=cursor or 0)
        for event in events:
            for index, mapped in enumerate(public_events(event)):
                kind, stage, payload = mapped
                await publisher.publish(
                    kind,
                    stage=stage,
                    payload=payload,
                    dedupe_key="agentscope:" + event["event_id"] + ":" + str(index),
                )
            async with self.transaction(lease) as (conn, _row):
                present = await conn.scalar(
                    select(self.cursors.c.sequence).where(
                        self.cursors.c.run_id == lease.run_id,
                        self.cursors.c.consumer == consumer,
                    )
                )
                if present is None:
                    await conn.execute(
                        insert(self.cursors).values(
                            run_id=lease.run_id,
                            consumer=consumer,
                            sequence=event["sequence"],
                        )
                    )
                elif present < event["sequence"]:
                    await conn.execute(
                        update(self.cursors)
                        .where(
                            self.cursors.c.run_id == lease.run_id,
                            self.cursors.c.consumer == consumer,
                        )
                        .values(sequence=event["sequence"])
                    )
        return len(events)

    async def register_task(self, lease, task_id):
        """Record run membership for feedback authorization, not a team scheduler."""
        async with self.transaction(lease) as (conn, row):
            if task_id not in row["task_ids"]:
                await conn.execute(
                    update(self.runs)
                    .where(self._identity(lease))
                    .values(task_ids=[*row["task_ids"], task_id])
                )

    async def request_cancel(self, run_id, user_id, command_id):
        """Persist user cancellation and revoke the active writer in one update."""
        async with self.engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        update(self.runs)
                        .where(
                            self.runs.c.run_id == run_id, self.runs.c.user_id == user_id
                        )
                        .values(revision=self.runs.c.revision + 1)
                        .returning(*self.runs.c)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise KeyError("run not found")
            existing = await conn.scalar(
                select(self.decisions.c.action_id).where(
                    self.decisions.c.run_id == run_id,
                    self.decisions.c.command_id == command_id,
                )
            )
            if existing:
                if existing != "run-cancel":
                    raise RecoveryConflict(
                        "cancel command ID was used for another action"
                    )
                return
            state = ResearchSnapshot.model_validate(row["snapshot"])
            if state.status in {"completed", "failed"}:
                raise RecoveryConflict("run already terminal")
            state.status, state.pending, state.inflight = "cancelled", None, None
            state.approvals.clear()
            state.error = "user_cancelled"
            configuration = state.application.get("configuration", {}).get("contract", {}).get("configurable", {})
            if self.engine.dialect.name == "postgresql" and configuration.get("async_research_mode") == "teams":
                # Same transaction as fence revocation: cancelled runs cannot leave a live board.
                await conn.execute(text("""UPDATE research_team_tasks SET status='cancelled',phase=NULL,version=version+1
                    WHERE run_id=:run AND status IN ('pending','running','waiting_for_confirmation')"""), {"run": run_id})
                await conn.execute(text("UPDATE research_team_plans SET status='superseded' WHERE run_id=:run AND status='pending'"), {"run": run_id})
                await conn.execute(text("UPDATE research_team_proposals SET status='cancelled' WHERE run_id=:run AND status='pending'"), {"run": run_id})
                await conn.execute(text("UPDATE research_team_members SET status='stopping',execution_token=NULL,lease_expires=NULL WHERE run_id=:run AND member_id<>'lead' AND status<>'closed'"), {"run": run_id})
                await conn.execute(text("UPDATE research_teams SET status='cancelled' WHERE run_id=:run"), {"run": run_id})
            await conn.execute(
                update(self.runs)
                .where(self.runs.c.run_id == run_id)
                .values(
                    snapshot=state.model_dump(mode="json"),
                    fence=row["fence"] + 1,
                    owner=None,
                    expires=0,
                )
            )
            await conn.execute(
                insert(self.decisions).values(
                    run_id=run_id,
                    command_id=command_id,
                    action_id="run-cancel",
                    payload={"action": "cancel"},
                    state="applied",
                )
            )
            await self._event(
                conn, row, "research.cancelled", {"command_id": command_id}
            )
