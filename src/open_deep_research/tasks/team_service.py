"""Transactional team and task operations shared by runtime and tools."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from open_deep_research.tasks.rocketmq_transport import RocketMQTransport
from open_deep_research.tasks.state import TaskSnapshot
from open_deep_research.tasks.team_protocol import MemberIdentity, TeamEvent


def task_view(row, *, summary=False):
    """Expose task facts, not Worker tokens or full model checkpoints."""
    termination = (row.get("result") or {}).get("termination")
    execution = {"termination": termination, "error_message": row.get("error_message")}
    if row.get("admission_status") == "rejected" and termination not in {None, "completed", "research_complete"}:
        execution["admission_reason"] = "research_execution_not_completed: " + termination
    elif row.get("admission_status") == "rejected":
        assessment = row.get("handoff_assessment") or {}
        execution["admission_reason"] = (
            "quality_evaluator_unavailable" if assessment.get("evaluator_error") else
            "; ".join(assessment.get("hard_rejection_reasons") or []) or assessment.get("reason", "quality_rejected")
        )[:1200]
    elif row.get("status") in {"failed", "cancelled", "timed_out"}:
        execution["admission_reason"] = row.get("error_message") or row["status"]
    if summary:
        keys = {"task_id", "owner", "status", "phase", "execution_mode", "version",
                "requirement_ids", "admission_status", "plan_version", "blocks",
                "blockedBy", "blocked_by", "unresolvedBlockedBy"}
        return {
            **execution,
            **{key: value for key, value in row.items() if key in keys},
            "id": row["task_id"],
            "subject": (row.get("display_title") or row.get("research_topic", ""))[:160],
        }
    keys = {"task_id", "display_title", "research_topic", "requirement_ids", "owner", "status",
            "phase", "execution_mode", "version", "admission_status", "plan_version", "metadata",
            "blocks", "blockedBy", "blocked_by", "unresolvedBlockedBy", "activeForm", "handoff_assessment", "error_message"}
    result = {key: value for key, value in row.items() if key in keys}
    result.update(execution)
    result.update(id=row["task_id"], subject=row.get("display_title") or row.get("research_topic", ""),
                  description=row.get("research_topic", ""))
    if row.get("result"):
        result["result"] = {key: row["result"].get(key) for key in ("compressed_research", "evidence_registry", "assessment", "termination")}
    return result


class TeamService:
    """Apply business rules inside the transaction checked by RocketMQ."""

    def __init__(self, transport: RocketMQTransport):
        """Share transport and its deployment-scoped PostgreSQL pool."""
        self.transport = transport
        self.store = transport.store

    async def command(
        self, identity: MemberIdentity, operation_id: str, kind: str,
        payload: dict[str, Any], *, fence_token: int,
    ) -> Any:
        """Execute one authenticated and idempotent coordination command."""
        event = TeamEvent(
            operation_id=operation_id, run_id=identity.run_id, sender=identity.member_id,
            recipients=["lead"], type=kind, payload=payload, fence_token=fence_token,
        )
        event.task_id = payload.get("task_id")
        try:
            from opentelemetry.propagate import inject
            inject(event.trace_context)
        except ImportError:
            pass
        if kind.startswith("task_"):
            async with self.store.pool.acquire() as db:
                members = await db.fetch(
                    "SELECT member_id FROM research_team_members WHERE run_id=$1 AND status<>'closed'",
                    identity.run_id,
                )
            event.recipients = sorted({"lead", *(row["member_id"] for row in members)})
            original = await self.store.prepared_event(identity.run_id, operation_id)
            if original is not None:
                event.recipients = original.recipients
        if kind in {"message", "send_message", "shutdown_request", "shutdown_response", "cancel_request"}:
            async with self.store.pool.acquire() as db:
                rows = await db.fetch(
                    """SELECT member_id FROM research_team_members WHERE run_id=$1
                       AND status<>'closed' AND ($2='*' OR name=$2 OR member_id=$2)""",
                    identity.run_id, payload["to"],
                )
            event.recipients = sorted(row["member_id"] for row in rows
                                      if payload["to"] != "*" or row["member_id"] != identity.member_id)
            original = await self.store.prepared_event(identity.run_id, operation_id)
            if original is not None:
                event.recipients = original.recipients
            if not event.recipients:
                raise ValueError("message_has_no_recipients")

        async def mutate(db):
            if kind == "team_create":
                if identity.role != "lead":
                    raise PermissionError("team_lead_required")
                await db.execute(
                    """INSERT INTO research_teams(run_id,name,fence_token,mode,execution_mode) VALUES($1,$2,$3,$4,$5)
                       ON CONFLICT(run_id) DO NOTHING""",
                    identity.run_id, payload["name"], fence_token,
                    payload.get("mode", "collaborator"), payload.get("execution_mode", "direct"),
                )
                await db.execute(
                    """INSERT INTO research_team_members(run_id,member_id,name)
                       VALUES($1,'lead','lead') ON CONFLICT DO NOTHING""", identity.run_id,
                )
                team = await db.fetchrow("SELECT status,fence_token FROM research_teams WHERE run_id=$1", identity.run_id)
                if team["status"] != "active" or team["fence_token"] != fence_token:
                    raise ValueError("team_not_active_or_stale_fence")
                return {"run_id": identity.run_id, "name": payload["name"]}
            # One team row serializes graph edits and capacity decisions. No
            # external call runs while this transaction holds the row lock.
            lock = " FOR UPDATE" if kind in {"member_spawn", "task_create", "task_dependencies", "task_update", "team_close"} else " FOR SHARE"
            team = await db.fetchrow("SELECT * FROM research_teams WHERE run_id=$1" + lock, identity.run_id)
            if not team or team["status"] != "active":
                raise ValueError("team_not_active")
            if team["fence_token"] != fence_token:
                raise RuntimeError("stale_team_fence")
            member = await db.fetchrow(
                "SELECT * FROM research_team_members WHERE run_id=$1 AND member_id=$2",
                identity.run_id, identity.member_id,
            )
            if not member or member["status"] == "closed":
                raise PermissionError("team_member_required")
            event.execution_epoch = member["execution_epoch"]
            if kind == "send_message":
                from open_deep_research.tasks.teams_domain import apply_message
                await apply_message(db, identity, event)
                return {"event_id": event.event_id, "recipients": event.recipients, "status": "accepted"}
            if kind == "task_plan_human":
                if identity.role != "lead":
                    raise PermissionError("team_lead_required")
                # Only the authenticated HTTP human interaction route exposes this command.
                row = await db.fetchrow(
                    """UPDATE research_team_tasks SET phase=$4,version=version+1,
                       plan_revision_limit=plan_version+4,
                       snapshot=jsonb_set(snapshot,'{_team_feedback}',
                         coalesce(snapshot->'_team_feedback','[]'::jsonb) || $5::jsonb)
                       WHERE run_id=$1 AND task_id=$2 AND version=$3 AND status='running'
                         AND phase='awaiting_human' RETURNING owner""",
                    identity.run_id, payload["task_id"], payload["version"], "planning",
                    json.dumps(["人工修订指导：" + payload["feedback"]]),
                )
                if row is None:
                    raise ValueError("plan_human_decision_conflict")
                event.recipients = ["lead", row["owner"]]
                return {"task_id": payload["task_id"], "phase": "planning"}
            if kind == "proposal_reject":
                if identity.role != "lead":
                    raise PermissionError("team_lead_required")
                changed = await db.fetchval(
                    "UPDATE research_team_proposals SET status='rejected',content=content || $3::jsonb WHERE run_id=$1 AND event_id=$2 AND status='pending' RETURNING event_id",
                    identity.run_id, payload["proposal_id"], json.dumps({"rejection_reason": payload["reason"]}),
                )
                if not changed:
                    raise ValueError("proposal_not_pending")
                return {"proposal_id": changed, "status": "rejected"}
            if kind in {"message", "shutdown_request", "shutdown_response", "cancel_request"}:
                if kind in {"shutdown_request", "cancel_request"} and identity.role != "lead":
                    raise PermissionError("team_lead_required")
                return {"event_id": event.event_id, "recipients": event.recipients}
            if kind == "member_spawn":
                if identity.role != "lead":
                    raise PermissionError("team_lead_required")
                count = await db.fetchval(
                    "SELECT count(*) FROM research_team_members WHERE run_id=$1 AND member_id<>'lead' AND status<>'closed'",
                    identity.run_id,
                )
                if count >= payload["max_members"]:
                    raise ValueError("team_capacity_exceeded")
                member_id = str(uuid4())
                await db.execute(
                    """INSERT INTO research_team_members(run_id,member_id,name,purpose,execution_mode,mode_override)
                       VALUES($1,$2,$3,$4,$5,$6)""",
                    identity.run_id, member_id, payload["name"], payload["purpose"],
                    payload.get("execution_mode") or team["execution_mode"], payload.get("execution_mode") is not None,
                )
                return {"member_id": member_id, "name": payload["name"]}
            if kind == "task_create":
                if team["mode"] == "teams" and identity.role != "lead":
                    raise PermissionError("members_propose_lead_creates")
                count = await db.fetchval(
                    """SELECT count(*) FROM research_team_tasks WHERE run_id=$1
                       AND ($2 OR status IN ('pending','running','waiting_for_confirmation'))""",
                    identity.run_id, payload.get("single_task", False),
                )
                if count >= (1 if payload.get("single_task") else payload["max_tasks"]):
                    raise ValueError("task_capacity_exceeded")
                snapshot = TaskSnapshot.model_validate(payload["snapshot"])
                if snapshot.run_id != identity.run_id:
                    raise PermissionError("task_run_mismatch")
                owner = None
                if payload.get("owner"):
                    owner = await db.fetchval("""SELECT member_id FROM research_team_members WHERE run_id=$1
                        AND (member_id=$2 OR name=$2) AND member_id<>'lead' AND status NOT IN ('closed','stopping','failed')""",
                        identity.run_id, payload["owner"])
                    if owner is None:
                        raise ValueError("unknown_member")
                task_data = snapshot.model_dump(mode="json")
                if payload.get("active_form"):
                    task_data["activeForm"] = payload["active_form"]
                await db.execute(
                    """INSERT INTO research_team_tasks
                       (run_id,task_id,snapshot,status,version,fence_token,owner,metadata)
                       VALUES($1,$2,$3::jsonb,'pending',1,$4,$5,$6::jsonb)""",
                    identity.run_id, snapshot.task_id, json.dumps(task_data), fence_token, owner, json.dumps(payload.get("metadata") or {}),
                )
                await self._dependencies(db, identity.run_id, snapshot.task_id, payload.get("blocked_by", []))
                if payload.get("proposal_id"):
                    changed = await db.fetchval(
                        """UPDATE research_team_proposals SET status='published',task_id=$3
                           WHERE run_id=$1 AND event_id=$2 AND status='pending' RETURNING event_id""",
                        identity.run_id, payload["proposal_id"], snapshot.task_id,
                    )
                    if not changed:
                        raise ValueError("proposal_not_pending")
                return {"task_id": snapshot.task_id, "status": "pending"}
            if team["mode"] == "teams" and kind in {"task_claim", "task_assign", "task_update"}:
                from open_deep_research.tasks.teams_domain import mutate_task, edit_dependencies
                result = await mutate_task(db, identity, kind, payload)
                if kind == "task_update":
                    await edit_dependencies(db, identity.run_id, payload["task_id"], payload)
                return result
            if kind == "task_claim":
                owner = payload.get("owner") or identity.member_id
                if identity.role != "lead" and owner != identity.member_id:
                    raise PermissionError("cannot_assign_another_member")
                target = await db.fetchval(
                    "SELECT member_id FROM research_team_members WHERE run_id=$1 AND (member_id=$2 OR name=$2) AND status<>'closed' AND member_id<>'lead'",
                    identity.run_id, owner,
                )
                if not target:
                    raise ValueError("unknown_member")
                task_id = payload["task_id"]
                snapshot = await db.fetchval(
                    """UPDATE research_team_tasks t SET owner=$3,version=version+1
                       WHERE run_id=$1 AND task_id=$2 AND status='pending' AND owner IS NULL
                       AND NOT EXISTS (
                           SELECT 1 FROM research_team_dependencies d
                           JOIN research_team_tasks b ON b.run_id=d.run_id AND b.task_id=d.blocker_id
                           WHERE d.run_id=t.run_id AND d.task_id=t.task_id
                           AND (b.status<>'completed' OR b.admission_status NOT IN ('accepted','accepted_with_caveats')))
                       AND NOT EXISTS (SELECT 1 FROM research_team_tasks a
                           WHERE a.run_id=$1 AND a.owner=$3 AND a.status IN ('pending','running','waiting_for_confirmation'))
                       RETURNING snapshot""", identity.run_id, task_id, target,
                )
                if snapshot is None:
                    raise ValueError("task_unavailable_or_member_busy")
                return {"task_id": task_id, "owner": target}
            if kind == "task_dependencies":
                if identity.role != "lead":
                    raise PermissionError("team_lead_required")
                await self._dependencies(db, identity.run_id, payload["task_id"], payload["blocked_by"])
                return {"task_id": payload["task_id"], "blocked_by": payload["blocked_by"]}
            if kind == "task_stop":
                if identity.role != "lead":
                    raise PermissionError("team_lead_required")
                status = await db.fetchval(
                    """UPDATE research_team_tasks SET status='cancelled',version=version+1,
                       snapshot=snapshot || '{"status":"cancelled"}'::jsonb
                       WHERE run_id=$1 AND task_id=$2 AND status IN ('pending','running','waiting_for_confirmation')
                       RETURNING status""", identity.run_id, payload["task_id"],
                )
                await db.execute("UPDATE research_team_plans SET status='superseded' WHERE run_id=$1 AND task_id=$2 AND status='pending'", identity.run_id, payload["task_id"])
                return {"task_id": payload["task_id"], "status": status or "already_terminal"}
            if kind == "team_close":
                if identity.role != "lead":
                    raise PermissionError("team_lead_required")
                active = await db.fetchval(
                    "SELECT count(*) FROM research_team_tasks WHERE run_id=$1 AND status IN ('pending','running','waiting_for_confirmation')",
                    identity.run_id,
                )
                if active:
                    raise ValueError("team_has_unfinished_tasks")
                if team["mode"] == "teams":
                    if await db.fetchval("SELECT 1 FROM research_team_members WHERE run_id=$1 AND member_id<>'lead' AND status NOT IN ('closed','failed') LIMIT 1", identity.run_id):
                        raise ValueError("team_members_not_stopped")
                    if await db.fetchval("SELECT 1 FROM research_team_proposals WHERE run_id=$1 AND status='pending' LIMIT 1", identity.run_id):
                        raise ValueError("team_has_pending_proposals")
                    if await db.fetchval("SELECT 1 FROM research_team_plans WHERE run_id=$1 AND status='pending' LIMIT 1", identity.run_id):
                        raise ValueError("team_has_pending_plans")
                await db.execute("UPDATE research_teams SET status='closed' WHERE run_id=$1", identity.run_id)
                await db.execute("UPDATE research_team_members SET status='closed' WHERE run_id=$1", identity.run_id)
                return {"status": "closed"}
            raise ValueError(f"unsupported_team_command:{kind}")

        return await self.transport.transact(event, mutate)

    async def _dependencies(self, db, run_id: str, task_id: str, blockers: list[str]) -> None:
        status = await db.fetchval(
            "SELECT status FROM research_team_tasks WHERE run_id=$1 AND task_id=$2", run_id, task_id,
        )
        if status != "pending":
            raise ValueError("only_pending_dependencies_can_change")
        for blocker in set(blockers):
            cycle = await db.fetchval(
                """WITH RECURSIVE ancestors(id) AS (
                       SELECT $3::text UNION SELECT d.blocker_id
                       FROM research_team_dependencies d JOIN ancestors a ON d.task_id=a.id
                       WHERE d.run_id=$1)
                   SELECT EXISTS(SELECT 1 FROM ancestors WHERE id=$2)""", run_id, task_id, blocker,
            )
            if cycle:
                raise ValueError("task_dependency_cycle")
            await db.execute(
                """INSERT INTO research_team_dependencies(run_id,task_id,blocker_id)
                   VALUES($1,$2,$3) ON CONFLICT DO NOTHING""", run_id, task_id, blocker,
            )

    async def tasks(self, run_id: str) -> list[dict[str, Any]]:
        """Return snapshots plus authoritative assignment and dependency fields."""
        async with self.store.pool.acquire() as db:
            rows = await db.fetch(
                """SELECT t.*,ARRAY(SELECT blocker_id FROM research_team_dependencies d
                   WHERE d.run_id=t.run_id AND d.task_id=t.task_id ORDER BY blocker_id) AS blocked_by,
                   ARRAY(SELECT task_id FROM research_team_dependencies d WHERE d.run_id=t.run_id AND d.blocker_id=t.task_id ORDER BY task_id) AS blocks,
                   ARRAY(SELECT d.blocker_id FROM research_team_dependencies d JOIN research_team_tasks b
                       ON b.run_id=d.run_id AND b.task_id=d.blocker_id WHERE d.run_id=t.run_id AND d.task_id=t.task_id
                       AND (b.status<>'completed' OR b.admission_status NOT IN ('accepted','accepted_with_caveats')) ORDER BY d.blocker_id) AS unresolved
                   FROM research_team_tasks t WHERE run_id=$1 ORDER BY created_at,task_id""", run_id,
            )
        return [dict(json.loads(row["snapshot"]), owner=row["owner"],
                     status=row["status"], admission_status=row["admission_status"],
                     version=row["version"], blocked_by=row["blocked_by"], blockedBy=row["blocked_by"],
                     blocks=row["blocks"], unresolvedBlockedBy=row["unresolved"], phase=row["phase"],
                     execution_mode=row["execution_mode"], plan_version=row["plan_version"], metadata=json.loads(row["metadata"])) for row in rows]
