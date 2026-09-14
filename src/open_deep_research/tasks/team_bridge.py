"""API-owned collaboration entry point for authenticated research Workers."""

from __future__ import annotations

import os
import secrets
import time
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field


class TeamWorkerRequest(BaseModel):
    """Identity is validated against the task token, never supplied as a member."""

    model_config = ConfigDict(extra="forbid")
    run_id: str
    task_id: str
    action: str
    payload: dict[str, Any] = Field(default_factory=dict)


async def worker_request(config, action: str, payload=None):
    """Use the existing short-lived task capability, without database credentials."""
    metadata = config["metadata"]
    request = TeamWorkerRequest(run_id=metadata["run_id"], task_id=metadata["task_id"],
                                action=action, payload=payload or {})
    async with httpx.AsyncClient(base_url=os.environ["SANDBOX_GATEWAY_URL"], timeout=60) as client:
        response = await client.post("/v1/team", json=request.model_dump(), headers={
            "Authorization": f"Bearer {os.environ['SANDBOX_TASK_TOKEN']}",
            "X-Sandbox-Timestamp": str(time.time()), "X-Sandbox-Nonce": secrets.token_urlsafe(24),
        })
        response.raise_for_status()
        return response.json()


async def member_dependencies(record):
    """Derive delegated coverage from the host-owned task, not Worker arguments."""
    from open_deep_research.quality.contract import (
        ResearchCoverageContract,
        ResearchRiskProfile,
    )
    from open_deep_research.tools.supervisor.deps import SupervisorToolDeps
    contract = (ResearchCoverageContract.model_validate(record.coverage_contract)
                if record.coverage_contract else None)
    if contract is not None:
        owned = set(record.requirement_ids)
        contract = contract.model_copy(update={
            "requirements": tuple(item for item in contract.requirements if item.requirement_id in owned),
        })
    return SupervisorToolDeps(
        enable_async_research=True,
        coverage_contract=contract,
        risk_profile=ResearchRiskProfile.model_validate(record.research_risk_profile)
        if record.research_risk_profile else ResearchRiskProfile(level="standard"),
    )


async def host_request(context, task_id: str, action: str, payload):
    """Resolve member identity from a live task after service authentication."""
    from open_deep_research.tasks.registry import get_task_registry
    from open_deep_research.tasks.team_protocol import MemberIdentity, member_identity
    from open_deep_research.tasks.team_runtime import team_runtime

    run_id = str(context.config.get("metadata", {}).get("run_id", "default"))
    record = get_task_registry().get(task_id)
    if (record is None or record.run_id != run_id or not record.assigned_teammate_id
            or record.status.value not in {"running", "waiting_for_confirmation"}):
        raise PermissionError("active_member_task_required")
    service = await team_runtime.start()
    async with service.store.pool.acquire() as db:
        name = await db.fetchval("SELECT name FROM research_team_members WHERE run_id=$1 AND member_id=$2",
                                 run_id, record.assigned_teammate_id)
    identity = MemberIdentity(run_id=run_id, member_id=record.assigned_teammate_id, name=name)
    token = member_identity.set(identity)
    try:
        if action == "input":
            from open_deep_research.tasks.team_members import MemberSessions
            session = await MemberSessions(service.store).load(run_id, identity.member_id)
            return {"messages": [item for item in session.get("messages", []) if item["type"] == "message"],
                    "cancelled": record.cancelled.is_set()}
        if action == "checkpoint":
            callback = checkpoint_callbacks.get((run_id, task_id))
            if callback is None:
                raise RuntimeError("research_checkpoint_sink_unavailable")
            from open_deep_research.agents.query_state import QueryLoopState
            await callback(QueryLoopState.from_snapshot(payload["query_state"]))
            return {"saved": True}
        from open_deep_research.tools.governance import (
            AgentRole,
            execute_governed_tool_call,
            filter_tools_by_permission,
        )
        from open_deep_research.tools.team import build_team_tools
        tools = build_team_tools(await member_dependencies(record), lead=False)
        tools = filter_tools_by_permission(tools, AgentRole.RESEARCHER, context.config)
        if context.configurable.sandbox_enabled:
            from open_deep_research.sandbox.schema import (
                resolve_profile,
                tool_policy_decision,
            )
            _, _, profile = resolve_profile(context.configurable)
            tools = [tool for tool in tools if tool_policy_decision(
                profile, tool_name=tool.name, effect=tool.effect.value,
            ) == "allow"]
        if action == "catalog":
            from open_deep_research.tools.base import tool_to_model_definition
            return {"tools": [{"name": tool.name, "definition": await tool_to_model_definition(tool),
                "origin": tool.origin.value, "effect": tool.effect.value, "retryable": tool.retryable,
                "concurrency_safe": tool.concurrency_safe, "max_output_chars": tool.max_output_chars,
                "prompt": tool.prompt(context.config)} for tool in tools]}
        if action == "tool":
            outcome = await execute_governed_tool_call(
                {"name": payload["tool_name"], "args": payload["arguments"], "id": payload["tool_call_id"]},
                {tool.name: tool for tool in tools}, AgentRole.RESEARCHER, context.config,
                operation_id=payload["logical_operation_id"], apply_retry=False,
            )
            return {"logical_operation_id": payload["logical_operation_id"], "tool_call_id": payload["tool_call_id"],
                    "status": "failed" if outcome.error else "completed",
                    "error": outcome.error.model_dump(mode="json") if outcome.error else None,
                    "output": outcome.result.output if outcome.result else outcome.message.content}
        raise ValueError("unknown_team_bridge_action")
    finally:
        member_identity.reset(token)


# Task-scoped callback lifetime matches run_task_with_control, including shutdown.
checkpoint_callbacks: dict[tuple[str, str], Any] = {}
