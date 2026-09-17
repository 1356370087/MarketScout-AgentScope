"""Native team and feedback HTTP contracts with SQL ownership checks."""

import json
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException

from open_deep_research.agentscope_runtime.run_config import RunConfig
from open_deep_research.api.contracts import HumanFeedbackRequest, TeamMessageRequest
from security.rbac.dependencies import require_permissions
from security.rbac.permissions import RESEARCH_RUN_INTERACT_OWN, RESEARCH_RUN_READ_OWN


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
            if host is None:
                raise HTTPException(503, "native_team_store_unavailable")
            async with host.pool.acquire() as db:
                team = await db.fetchrow(
                    "SELECT name,status FROM research_teams WHERE run_id=$1", run_id
                )
                members = await db.fetch(
                    "SELECT member_id,name,purpose,status FROM research_team_members WHERE run_id=$1 ORDER BY name",
                    run_id,
                )
                tasks = await db.fetch(
                    """SELECT t.*, ARRAY(SELECT blocker_id FROM research_team_dependencies d
                    WHERE d.run_id=t.run_id AND d.task_id=t.task_id) AS blocked_by
                    FROM research_team_tasks t WHERE run_id=$1 ORDER BY created_at,task_id""",
                    run_id,
                )
                events = await db.fetch(
                    "SELECT event FROM research_coordination_events WHERE run_id=$1 AND event->>'type'='message' ORDER BY sequence DESC LIMIT 50",
                    run_id,
                )
            keys = {
                "task_id",
                "display_title",
                "status",
                "owner",
                "admission_status",
                "blocked_by",
                "error_message",
            }
            return {
                "enabled": True,
                **(dict(team) if team else {}),
                "members": [dict(row) for row in members],
                "tasks": [
                    {
                        k: v
                        for k, v in dict(
                            json.loads(row["snapshot"]),
                            owner=row["owner"],
                            status=row["status"],
                            admission_status=row["admission_status"],
                            blocked_by=row["blocked_by"],
                        ).items()
                        if k in keys
                    }
                    for row in tasks
                ],
                "messages": [json.loads(row["event"]) for row in reversed(events)],
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
            return await team.say(
                team.leader, "human:" + request.command_id, request.to, request.message
            )

    return router
