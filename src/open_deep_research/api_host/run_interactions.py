"""Human interactions and commands for the legacy research host."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from open_deep_research.api.contracts import HumanActionRequest, HumanFeedbackRequest, TeamMessageRequest
from open_deep_research.run_context import RunContextStore
from open_deep_research.run_control import RunControlStore
from security.rbac import Principal, require_permissions
from security.rbac.permissions import RESEARCH_RUN_CONTROL_OWN, RESEARCH_RUN_INTERACT_OWN, RESEARCH_RUN_READ_OWN

logger = logging.getLogger(__name__)


class RunInteractionRoutes:
    """Dispatch owned commands without owning the host's runtime registry."""

    def __init__(self, *, require_run_owner):
        self.require_run_owner = require_run_owner
        self.router = APIRouter()
        self.router.add_api_route("/runs/{run_id}/human-actions/{action_id}", self.submit_human_action, methods=["POST"])
        self.router.add_api_route("/runs/{run_id}/feedback", self.submit_feedback, methods=["POST"])
        self.router.add_api_route("/runs/{run_id}/team", self.get_research_team, methods=["GET"])
        self.router.add_api_route("/runs/{run_id}/team/messages", self.send_team_message, methods=["POST"])
        self.router.add_api_route("/runs/{run_id}/cancel", self.cancel_run, methods=["POST"])

    async def submit_human_action(
        self,
        run_id: str,
        action_id: str,
        request: HumanActionRequest,
        user: Principal = Depends(require_permissions(RESEARCH_RUN_INTERACT_OWN.code)),
    ) -> dict[str, Any]:
        """Resolve a pending human approval, revision, or cancellation action."""
        record, configurable = self.require_run_owner(run_id, user)
        if record is not None:
            try:
                result = record.engine.handle_human_action(action_id, request.action, request.message or "")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if request.action == "cancel":
                record.status = "cancelled"
            return result
        manifest = RunContextStore(run_id, runs_dir=configurable.runs_dir).load_manifest()
        pending = manifest.pending_human_action or {}
        if pending.get("action_id") != action_id:
            raise HTTPException(status_code=400, detail="No matching pending human action")
        allowed = (
            {"answer", "cancel"}
            if pending.get("type") == "clarification"
            else {"approve", "deny", "cancel"}
            if pending.get("type") == "fetch_budget_approval"
            else {"approve", "revise", "cancel"}
        )
        if request.action not in allowed:
            raise HTTPException(
                status_code=400,
                detail="Human action does not match the pending action type",
            )
        if request.action in {"answer", "revise"} and not (request.message or "").strip():
            raise HTTPException(status_code=400, detail="A message is required for this human action")
        command = await RunControlStore(run_id, runs_dir=configurable.runs_dir).enqueue(
            "human_action",
            {"action_id": action_id, "action": request.action, "message": request.message or ""},
            command_id=f"human-action-{action_id}",
        )
        return {"status": "accepted", "command_id": command.command_id, "action": request.action}


    async def submit_feedback(
        self,
        run_id: str,
        request: HumanFeedbackRequest,
        user: Principal = Depends(require_permissions(RESEARCH_RUN_INTERACT_OWN.code)),
    ) -> dict[str, Any]:
        """Accept mid-run human direction or evidence questions."""
        record, configurable = self.require_run_owner(run_id, user)
        payload = request.model_dump(exclude_none=True)
        if record is not None:
            try:
                return await record.engine.submit_feedback(payload)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        command = await RunControlStore(run_id, runs_dir=configurable.runs_dir).enqueue(
            "feedback",
            payload,
            command_id=request.command_id,
        )
        return {"status": "accepted", "command_id": command.command_id}


    async def get_research_team(
        self,
        run_id: str,
        user: Principal = Depends(require_permissions(RESEARCH_RUN_READ_OWN.code)),
    ) -> dict[str, Any]:
        """Return the owned team's shared task list and bounded message history."""
        _record, configurable = self.require_run_owner(run_id, user)
        if not configurable.enable_async_research:
            return {"enabled": False}
        from open_deep_research.tasks.team_runtime import team_runtime
        service = await team_runtime.start()
        async with service.store.pool.acquire() as db:
            team = await db.fetchrow("SELECT name,status FROM research_teams WHERE run_id=$1", run_id)
            if team is None:
                return {"enabled": True, "members": [], "tasks": [], "messages": []}
            members = await db.fetch("SELECT member_id,name,purpose,status FROM research_team_members WHERE run_id=$1 ORDER BY name", run_id)
            messages = await db.fetch("""SELECT event FROM research_coordination_events
                WHERE run_id=$1 AND event->>'type'='message' ORDER BY sequence DESC LIMIT 50""", run_id)
        tasks = await service.tasks(run_id)
        keys = {"task_id", "display_title", "status", "owner", "admission_status", "blocked_by", "error_message"}
        return {"enabled": True, **dict(team), "members": [dict(row) for row in members],
                "tasks": [{key: value for key, value in task.items() if key in keys} for task in tasks],
                "messages": [json.loads(row["event"]) for row in reversed(messages)]}


    async def send_team_message(
        self,
        run_id: str, request: TeamMessageRequest,
        user: Principal = Depends(require_permissions(RESEARCH_RUN_INTERACT_OWN.code)),
    ) -> dict[str, Any]:
        """Send human instructions through the same transaction and recipient rules."""
        record, configurable = self.require_run_owner(run_id, user)
        if record is None or not configurable.enable_async_research:
            raise HTTPException(status_code=409, detail="team_run_not_active")
        from open_deep_research.tasks.team_protocol import MemberIdentity
        from open_deep_research.tasks.team_runtime import team_runtime
        service = await team_runtime.start()
        try:
            return await service.command(MemberIdentity(run_id=run_id, member_id="lead", name="lead", role="lead"),
                f"human:{request.command_id}", "message", {"to": request.to, "message": request.message},
                fence_token=int(record.engine.config["metadata"]["run_fence_token"]))
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


    async def cancel_run(
        self,
        run_id: str,
        user: Principal = Depends(require_permissions(RESEARCH_RUN_CONTROL_OWN.code)),
    ) -> dict[str, str]:
        """Cancel a background run."""
        record, configurable = self.require_run_owner(run_id, user)
        terminal_statuses = {"completed", "failed", "cancelled"}
        if record is not None:
            if record.status in terminal_statuses:
                return {"run_id": run_id, "status": record.status}
            record.engine.interrupt()
            record.status = "cancelling"
        else:
            manifest = RunContextStore(
                run_id,
                runs_dir=configurable.runs_dir,
            ).load_manifest()
            if manifest.status in terminal_statuses:
                return {"run_id": run_id, "status": manifest.status}
            await RunControlStore(run_id, runs_dir=configurable.runs_dir).enqueue(
                "cancel",
                {},
                command_id=f"cancel-{run_id}",
            )
        logger.info(
            "run cancellation requested",
            extra={"actor": user.user_id, "action": "run.cancel", "run_id": run_id},
        )
        return {"run_id": run_id, "status": "cancelling"}


