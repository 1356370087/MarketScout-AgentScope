"""Team tools backed by authenticated, idempotent host commands."""

from __future__ import annotations

from functools import partial
from uuid import NAMESPACE_URL, uuid5

from open_deep_research.configuration import Configuration
from open_deep_research.tasks.state import TaskSnapshot
from open_deep_research.tasks.team_protocol import MemberIdentity, member_identity
from open_deep_research.tasks.team_runtime import team_runtime
from open_deep_research.tools.base import (
    ToolEffect,
    ToolExecutionZone,
    ToolOrigin,
    ToolResult,
    build_tool,
)
from open_deep_research.tools.supervisor.common import validate_requirement_ids

MEMBER_TOOL_NAMES = frozenset({"TaskCreate", "TaskGet", "TaskList", "TaskUpdate", "SendMessage"})

async def _call(name, deps, input, context, on_progress=None):
    del on_progress
    config = Configuration.from_runnable_config(context.config)
    metadata = context.config.get("metadata", {})
    run_id = str(metadata.get("run_id", "default"))
    identity = member_identity.get()
    if identity is None:
        if context.role != "supervisor":
            raise PermissionError("authenticated_member_identity_required")
        identity = MemberIdentity(run_id=run_id, member_id="lead", name="lead", role="lead")
    if identity.run_id != run_id:
        raise PermissionError("team_run_mismatch")
    service = await team_runtime.start()
    if identity.role == "lead":
        from open_deep_research.tasks.registry import get_task_registry
        from open_deep_research.tasks.teammate_pool import get_teammate_pool
        pool = get_teammate_pool(context.config, get_task_registry(), deps.researcher_ainvoke)
        await pool.start()
    operation = context.operation_id or f"{identity.member_id}:{context.tool_call_id}"
    fence = int(metadata.get("run_fence_token", 0))
    payload = input.model_dump()
    if name in {"TaskList", "TaskGet"}:
        tasks = await service.tasks(run_id)
        keys = ("task_id", "display_title", "status", "owner", "blocked_by", "admission_status", "requirement_ids")
        if name == "TaskList":
            return ToolResult(output=[{key: item.get(key) for key in keys} for item in tasks])
        item = next((item for item in tasks if item["task_id"] == input.task_id), None)
        if item is None:
            return ToolResult(output=None)
        return ToolResult(output={
            **{key: item.get(key) for key in keys},
            "description": item.get("research_topic", ""), "error": item.get("error_message"),
            "summary": str((item.get("result") or {}).get("compressed_research", ""))[:12000],
            "artifact_path": item.get("result_artifact_path"),
        })
    if name == "WaitForTeamEvents":
        from open_deep_research.tasks.team_inbox import TeamInbox
        messages = await TeamInbox(run_id).wait_and_claim(
            agent_id=identity.member_id, consumer_id="", timeout_seconds=input.timeout_seconds,
        )
        return ToolResult(output={"available": len(messages)})
    kind = {"TeamCreate": "team_create", "SpawnTeammate": "member_spawn",
            "TaskCreate": "task_create", "SendMessage": "message", "TeamDelete": "team_close",
            "TaskStop": "task_stop"}.get(name)
    if name == "SpawnTeammate":
        payload["max_members"] = config.max_persistent_teammates
    elif name == "TaskCreate":
        contract = deps.coverage_contract
        ids = validate_requirement_ids(input.requirement_ids, contract, required=contract is not None)
        snapshot = TaskSnapshot(
            task_id=str(uuid5(NAMESPACE_URL, f"{run_id}:{operation}")), run_id=run_id,
            research_topic=input.description, display_title=input.subject,
            plan_task_id=context.tool_call_id, requirement_ids=ids,
            wave_id=str(metadata.get("research_wave_id", "wave-0")),
            user_id=metadata.get("user_id"),
            coverage_contract=contract.model_dump() if contract else {},
            research_risk_profile=deps.risk_profile.model_dump(), fence_token=fence,
        )
        # Stable timestamps are derived once: retries reuse the prepared input.
        previous = await service.store.prepared_event(run_id, operation)
        if previous:
            snapshot.created_at = previous.payload["snapshot"]["created_at"]
            snapshot.updated_at = previous.payload["snapshot"]["updated_at"]
            snapshot.fence_token = previous.payload["snapshot"]["fence_token"]
        payload = {"snapshot": snapshot.model_dump(mode="json"), "blocked_by": input.blocked_by,
                   "max_tasks": config.max_in_flight_tasks,
                   "single_task": bool(contract and contract.single_research_task)}
    elif name == "TaskUpdate":
        kind = "task_claim" if input.action == "claim" else "task_dependencies"
        if input.action in {"instructions", "request_completion"}:
            tasks = await service.tasks(run_id)
            task = next((item for item in tasks if item["task_id"] == input.task_id), None)
            if task is None or not task["owner"]:
                raise ValueError("task_has_no_owner")
            if identity.role != "lead" and task["owner"] != identity.member_id:
                raise PermissionError("cannot_update_another_member_task")
            if input.action == "instructions" and not input.instruction.strip():
                raise ValueError("instruction_required")
            kind = "message"
            payload = {"to": task["owner"], "task_id": input.task_id,
                       "message": input.instruction if input.action == "instructions" else
                       "Please assess whether this task is ready to complete. Run the normal completion and evidence checks; do not bypass admission."}
    result = await service.command(identity, operation, kind, payload, fence_token=fence)
    if name == "SpawnTeammate":
        await pool.refresh_members()
    return ToolResult(output=result)


def build_team_tool(name, schema, description, guidance, deps):
    """Apply the common trusted execution policy to one folder-owned tool."""
    return build_tool(
        name=name, input_schema=schema, description=description,
        call=partial(_call, name, deps), origin=ToolOrigin.SYSTEM,
        effect=ToolEffect.READ_ONLY if name in {"TaskGet", "TaskList", "WaitForTeamEvents"} else ToolEffect.COORDINATION_WRITE,
        execution_zone=ToolExecutionZone.HOST_CONTROL,
        supports_idempotency=True, concurrency_safe=True, prompt=guidance,
    )


def build_team_tools(deps, *, lead=True):
    """Assemble folder-owned tools permitted for this agent's team role."""
    from importlib import import_module
    folders = ["task_create", "task_get", "task_list", "task_update", "send_message"]
    if lead:
        folders += ["team_create", "spawn_teammate", "team_delete", "task_stop", "wait_for_team_events"]
    return [import_module(f"open_deep_research.tools.team.{folder}.definition").build(deps) for folder in folders]
