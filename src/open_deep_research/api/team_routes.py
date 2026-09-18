"""Native team and feedback HTTP contracts with SQL ownership checks."""

import json
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from open_deep_research.agentscope_runtime.run_config import RunConfig
from open_deep_research.api.contracts import HumanFeedbackRequest, TeamMessageRequest
from security.rbac.dependencies import require_permissions
from security.rbac.permissions import RESEARCH_RUN_INTERACT_OWN, RESEARCH_RUN_READ_OWN


class PlanRevisionRequest(BaseModel):
    version: int
    feedback: str = Field(min_length=1, max_length=12000)
    command_id: str


def build_team_router(service):
    from open_deep_research.api.research_router import http_errors

    router = APIRouter()
    read = Depends(require_permissions(RESEARCH_RUN_READ_OWN.code))
    interact = Depends(require_permissions(RESEARCH_RUN_INTERACT_OWN.code))

    @router.get("/runs/{run_id}/budget")
    async def budget(run_id: str, principal=read):
        with http_errors():
            return await service.store.budget(run_id, principal.user_id)

    @router.post("/runs/{run_id}/feedback")
    async def feedback(run_id: str, request: HumanFeedbackRequest, principal=interact):
        with http_errors():
            state, _ = await service.store.load(run_id, principal.user_id)
            if state.status in {"completed", "failed", "cancelled"}:
                raise HTTPException(409, "run_not_active")
            if not request.message.strip():
                raise HTTPException(400, "feedback_message_empty")
            command_id = request.command_id or uuid4().hex
            task_id = request.task_id or "supervisor"
            message = request.message
            if request.type == "evidence_question":
                message = json.dumps(
                    request.model_dump(exclude_none=True), ensure_ascii=False
                )
            team = (
                getattr(service.pipeline_factory, "active", {})
                .get(run_id, {})
                .get("team")
            )
            # Infrastructure can exist before Lead explicitly calls TeamCreate.
            # Until then, retain feedback through the normal run checkpoint path.
            if team is not None and not await team.members():
                team = None
            if team is not None:
                recipient = "lead"
                if request.task_id:
                    task = next(
                        (
                            row
                            for row in await team.service.tasks(run_id)
                            if row["task_id"] == request.task_id
                        ),
                        None,
                    )
                    if task is None or not task["owner"]:
                        raise HTTPException(409, "feedback_task_not_assigned")
                    recipient = task["owner"]
                await team.say(
                    team.leader, "feedback:" + command_id, recipient, message
                )
                return {"status": "accepted", "command_id": command_id}
            await service.store.submit_decision(
                run_id,
                principal.user_id,
                command_id,
                "feedback:" + task_id,
                {"action": "feedback", "task_id": task_id, "feedback": message},
            )
            return {"status": "accepted", "command_id": command_id}

    @router.get("/runs/{run_id}/team")
    async def team_view(run_id: str, principal=read):
        with http_errors():
            state, _ = await service.store.load(run_id, principal.user_id)
            run = RunConfig.restore(state.application["configuration"])
            if not run.get("enable_async_research"):
                return {"enabled": False}
            runtime = getattr(service.pipeline_factory, "runtime", None)
            host = getattr(runtime, "_team_host", None)
            if host is None and runtime is not None:
                host = await runtime.team_host()
            if host is None:
                raise HTTPException(503, "native_team_store_unavailable")
            async with host.pool.acquire() as db:
                team = await db.fetchrow(
                    "SELECT name,status,mode,execution_mode FROM research_teams WHERE run_id=$1", run_id
                )
                members = await db.fetch(
                    "SELECT member_id,name,purpose,status,current_task_id,execution_mode,mode_override FROM research_team_members WHERE run_id=$1 ORDER BY name",
                    run_id,
                )
                events = await db.fetch(
                    """SELECT e.event,e.sequence,
                       CASE WHEN EXISTS(SELECT 1 FROM research_coordination_receipts r WHERE r.event_id=e.event_id)
                         THEN CASE WHEN EXISTS(SELECT 1 FROM research_coordination_receipts r WHERE r.event_id=e.event_id AND NOT r.applied)
                              THEN 'delivered' ELSE 'applied' END
                         ELSE 'accepted' END AS delivery_status
                       FROM research_coordination_events e WHERE run_id=$1 AND event->>'type' IN ('message','send_message') ORDER BY sequence DESC LIMIT 50""",
                    run_id,
                )
                plans = await db.fetch("SELECT task_id,version,owner,request_id,content,status,feedback,reviewed_by FROM research_team_plans WHERE run_id=$1 ORDER BY task_id,version", run_id)
                proposals = await db.fetch("SELECT event_id,member_id,content,status,task_id FROM research_team_proposals WHERE run_id=$1 ORDER BY created_at", run_id)
                metrics = await db.fetchrow("""SELECT
                    (SELECT count(*) FROM research_coordination_transactions WHERE run_id=$1 AND event->>'type'='task_claim' AND result->>'claimed'='false') AS claim_conflicts,
                    (SELECT count(*) FROM research_coordination_outbox o JOIN research_coordination_events e USING(event_id) WHERE e.run_id=$1 AND o.published_at IS NULL) AS message_backlog,
                    (SELECT coalesce(sum(greatest(execution_epoch-1,0)),0) FROM research_team_members WHERE run_id=$1) AS member_recoveries,
                    (SELECT avg(extract(epoch FROM reviewed_at-created_at)) FROM research_team_plans WHERE run_id=$1 AND reviewed_at IS NOT NULL) AS plan_review_seconds
                    """, run_id)
            from types import SimpleNamespace
            from open_deep_research.tasks.team_service import TeamService, task_view
            from open_deep_research.tasks.team_store import TeamStore
            tasks = await TeamService(SimpleNamespace(store=TeamStore(host.pool))).tasks(run_id)
            return {
                "enabled": True,
                "mode": run.get("async_research_mode"),
                "execution_mode": run.get("team_execution_mode"),
                **(dict(team) if team else {}),
                "members": [dict(row) for row in members],
                "tasks": [{**task_view(row), "error_message": row.get("error_message")} for row in tasks],
                "messages": [{**json.loads(row["event"]), "sequence": row["sequence"], "delivery_status": row["delivery_status"]} for row in reversed(events)],
                "plans": [{**dict(row), "content": json.loads(row["content"])} for row in plans],
                "proposals": [{**dict(row), "content": json.loads(row["content"])} for row in proposals],
                "metrics": dict(metrics),
            }

    @router.post("/runs/{run_id}/team/messages")
    async def team_message(
        run_id: str, request: TeamMessageRequest, principal=interact
    ):
        with http_errors():
            await service.store.load(run_id, principal.user_id)
            active = getattr(service.pipeline_factory, "active", {}).get(run_id, {})
            team = active.get("team")
            if team is None:
                raise HTTPException(409, "team_run_not_active")
            if team.transport.reliable or not isinstance(request.message, str):
                return await team.send_message(team.leader, "human:" + request.command_id, request.to, request.message, request.summary)
            return await team.say(team.leader, "human:" + request.command_id, request.to, request.message)

    @router.get("/runs/{run_id}/team/messages")
    async def messages(run_id: str, after: int = 0, principal=read):
        await service.store.load(run_id, principal.user_id)
        host = getattr(getattr(service.pipeline_factory, "runtime", None), "_team_host", None)
        if host is None and getattr(service.pipeline_factory, "runtime", None) is not None:
            host = await service.pipeline_factory.runtime.team_host()
        if host is None:
            raise HTTPException(503, "native_team_store_unavailable")
        async with host.pool.acquire() as db:
            rows = await db.fetch("""SELECT e.sequence,e.event,
                CASE WHEN EXISTS(SELECT 1 FROM research_coordination_receipts r WHERE r.event_id=e.event_id)
                  THEN CASE WHEN EXISTS(SELECT 1 FROM research_coordination_receipts r WHERE r.event_id=e.event_id AND NOT r.applied)
                       THEN 'delivered' ELSE 'applied' END ELSE 'accepted' END AS delivery_status
                FROM research_coordination_events e WHERE run_id=$1 AND sequence>$2
                  AND event->>'type' IN ('message','send_message')
                ORDER BY sequence LIMIT 100""", run_id, after)
        return {"items": [{"sequence": row["sequence"], **json.loads(row["event"]), "delivery_status": row["delivery_status"]} for row in rows], "cursor": rows[-1]["sequence"] if rows else after}

    @router.get("/runs/{run_id}/team/tasks/{task_id}")
    async def task_detail(run_id: str, task_id: str, principal=read):
        data = await team_view(run_id, principal)
        result = next((row for row in data.get("tasks", []) if row["task_id"] == task_id), None)
        if result is None:
            raise HTTPException(404, "task_not_found")
        return {**result, "plans": [p for p in data["plans"] if p["task_id"] == task_id]}

    from open_deep_research.agentscope_runtime.teams_tools import TaskUpdateInput

    @router.post("/runs/{run_id}/team/tasks/{task_id}/plan-revision")
    async def revise_plan(run_id: str, task_id: str, request: PlanRevisionRequest, principal=interact):
        with http_errors():
            await service.store.load(run_id, principal.user_id)
            team = getattr(service.pipeline_factory, "active", {}).get(run_id, {}).get("team")
            if team is None:
                raise HTTPException(409, "team_run_not_active")
            return await team.command("human-plan:" + request.command_id, "task_plan_human", {
                "task_id": task_id, "version": request.version, "feedback": request.feedback,
                "user_id": principal.user_id,
            })

    @router.post("/runs/{run_id}/team/tasks/{task_id}")
    async def update_task(run_id: str, task_id: str, request: TaskUpdateInput, principal=interact):
        with http_errors():
            await service.store.load(run_id, principal.user_id)
            if request.task_id != task_id:
                raise HTTPException(400, "task_id_mismatch")
            team = getattr(service.pipeline_factory, "active", {}).get(run_id, {}).get("team")
            if team is None:
                raise HTTPException(409, "team_run_not_active")
            if request.owner and any((request.addBlocks, request.addBlockedBy, request.removeBlocks, request.removeBlockedBy)):
                raise HTTPException(400, "assign_and_dependency_edit_require_separate_versions")
            kind = "task_assign" if request.owner else "task_update"
            return await team.command("human-task:" + uuid4().hex, kind, request.model_dump(exclude_none=True))

    return router
