"""Local evaluation research through the same durable AgentScope service as API."""

import asyncio
import os
from contextlib import AsyncExitStack
from pathlib import Path

from open_deep_research.agentscope_runtime.native_host import (
    build_native_research_service,
)
from open_deep_research.api.contracts import RunRequest


async def evaluation_principal():
    """Use existing local-dev auth or a live IAM token, never invent privileges."""
    from security.rbac.dependencies import get_current_principal, require_permissions
    from security.rbac.permissions import RESEARCH_RUN_CREATE

    token = os.getenv("EVALUATION_ACCESS_TOKEN", "").strip()
    principal = await get_current_principal("Bearer " + token if token else None)
    return await require_permissions(RESEARCH_RUN_CREATE.code)(principal)


def evaluation_state(snapshot):
    """Project persisted native domain data, preserving failures over stale output."""
    product = snapshot.report_product
    result = dict(product.get("result") or {})
    if snapshot.status != "completed":
        result = {
            "status": "cancelled" if snapshot.status == "cancelled" else "error",
            "error": snapshot.error or "native_run_" + snapshot.status,
        }
    elif not result:
        result = {
            "status": "partial"
            if product.get("quality_gate", {}).get("status") in {"failed", "degraded"}
            else "success"
        }
    from open_deep_research.quality.planning import unique_evidence
    return {
        "engine": "agentscope",
        "runtime_status": snapshot.status,
        "research_brief": snapshot.research_brief,
        "final_report": snapshot.final_report,
        "result": result,
        "coverage_contract": snapshot.coverage_contract,
        "coverage_ledger": snapshot.coverage_ledger,
        "completed_task_outputs": snapshot.findings,
        "supervisor_messages": snapshot.agent_states.get("supervisor", {}).get("context", []),
        "evidence_registry": unique_evidence([record for task in snapshot.findings for record in task.get("evidence_registry", [])]),
        "evaluation_snapshot": product.get("evaluation_snapshot"),
        "coverage_checklist": product.get("coverage_checklist", []),
        "quality_gate": product.get("quality_gate"),
    }


async def run_native_question(
    messages,
    config,
    *,
    runs_dir,
    timeout=1800,
    resource_provider=None,
    interactions=(),
    trusted_configuration=None,
):
    """Create one native run, await its executor and close all owned resources.

    A waiting run is recorded as requiring interaction, never auto-approved.
    Only this invocation's new run is cancelled on timeout or caller cancellation.
    """
    if timeout <= 0:
        raise ValueError("evaluation timeout must be positive")
    principal = await evaluation_principal()
    if interactions and resource_provider is None:
        raise ValueError("scripted_approvals_require_isolated_fixture_resources")
    options = {"runs_dir": Path(runs_dir)}
    if resource_provider is not None:
        options["resource_provider"] = resource_provider
    if (
        resource_provider is None
        and os.getenv("EVALUATION_LOCAL_WORKERS", "false").lower() == "true"
    ):
        options["external_workers"] = False
    service = await build_native_research_service(**options)
    stack = AsyncExitStack()
    prepare = service.prepare_config

    async def prepare_evaluation(request, principal):
        prepared = await prepare(request, principal)
        if trusted_configuration:
            prepared["configurable"].update(trusted_configuration)
        return {**prepared, "evaluation_capture": True}

    service.prepare_config = prepare_evaluation

    async def project(snapshot):
        from .trace import collect_native_snapshot

        state = evaluation_state(snapshot)
        state["evaluation_snapshot"] = await collect_native_snapshot(
            service.store, snapshot, principal.user_id
        )
        runtime = getattr(service.pipeline_factory, "runtime", None)
        team_host = getattr(runtime, "_team_host", None)
        if team_host is not None:
            async with team_host.pool.acquire() as connection:
                rows = await connection.fetch(
                    "SELECT task_id, status, snapshot->>'_worker_error' AS error FROM research_team_tasks WHERE run_id=$1",
                    snapshot.run_id,
                )
            failures = [dict(row) for row in rows if row["error"]]
            state["evaluation_snapshot"]["outcome"]["worker_failures"] = failures
            from .graders import INFRASTRUCTURE_ERRORS

            if (
                snapshot.status == "failed"
                and rows
                and all(
                    row["status"] == "failed" and row["error"] in INFRASTRUCTURE_ERRORS
                    for row in rows
                )
            ):
                state["evaluation_error"] = "worker_infrastructure_unavailable"
        return state

    run_id = None
    try:
        if resource_provider is None and os.getenv("EVALUATION_CALLBACK_HOST"):
            from .callbacks import gateway_callbacks

            await stack.enter_async_context(
                gateway_callbacks(
                    service,
                    host=os.environ["EVALUATION_CALLBACK_HOST"],
                    port=int(os.getenv("EVALUATION_CALLBACK_PORT", "2024")),
                )
            )
        request = RunRequest(messages=messages, configurable=config["configurable"])
        run_id = await service.create(request, principal)
        try:
            async with asyncio.timeout(timeout):
                task = service.tasks.get(run_id)
                if task is not None:
                    await asyncio.shield(task)
                for interaction in interactions:
                    parked, _ = await service.store.load(run_id, principal.user_id)
                    if parked.status != "waiting":
                        raise ValueError("scripted_interaction_expected_waiting_run")
                    if interaction["stage"] == "tool":
                        matches = [
                            key
                            for key, approval in parked.approvals.items()
                            if approval["kind"] == "tool"
                            and approval["payload"].get("tool_name")
                            == interaction.get("tool_name")
                        ]
                        if len(matches) != 1:
                            raise ValueError("scripted_tool_approval_mismatch")
                        action_id = matches[0]
                    else:
                        if (
                            parked.pending is None
                            or parked.pending.stage != interaction["stage"]
                        ):
                            raise ValueError("scripted_interaction_stage_mismatch")
                        action_id = parked.pending.id
                    await service.decide(
                        run_id,
                        principal.user_id,
                        action_id,
                        interaction["action"],
                        interaction.get("message", ""),
                    )
                    task = service.tasks.get(run_id)
                    if task is not None:
                        await asyncio.shield(task)
        except TimeoutError:
            await service.cancel(run_id, principal.user_id)
            snapshot, _ = await service.store.load(run_id, principal.user_id)
            state = await project(snapshot)
            state["result"] = {
                "status": "error",
                "error": "evaluation_research_timeout",
            }
            return run_id, state
        except asyncio.CancelledError:
            await service.cancel(run_id, principal.user_id)
            raise
        snapshot, _ = await service.store.load(run_id, principal.user_id)
        from open_deep_research.agentscope_runtime.run_config import RunConfig

        frozen = RunConfig.restore(snapshot.application["configuration"])
        state = await project(snapshot)
        state["evaluation_configuration"] = {
            key: frozen.get(key)
            for key in config["configurable"]
            if key in frozen.compatibility_projection().get("configurable", {})
        }
        return run_id, state
    finally:
        try:
            await service.native_aclose()
        finally:
            await stack.aclose()
