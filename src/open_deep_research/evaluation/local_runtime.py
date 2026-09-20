"""Local evaluation research through the same durable AgentScope service as API."""

import asyncio
import os
from pathlib import Path

from open_deep_research.agentscope_runtime.native_host import build_native_research_service
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
        result = {"status": "cancelled" if snapshot.status == "cancelled" else "error",
                  "error": snapshot.error or "native_run_" + snapshot.status}
    elif not result:
        result = {"status": "partial" if product.get("quality_gate", {}).get("status") in {"failed", "degraded"} else "success"}
    return {
        "engine": "agentscope", "runtime_status": snapshot.status,
        "research_brief": snapshot.research_brief,
        "final_report": snapshot.final_report,
        "result": result,
        "coverage_contract": snapshot.coverage_contract,
        "coverage_ledger": snapshot.coverage_ledger,
        "completed_task_outputs": snapshot.findings,
        "evidence_registry": [record for task in snapshot.findings for record in task.get("evidence_registry", [])],
        "evaluation_snapshot": product.get("evaluation_snapshot"),
        "coverage_checklist": product.get("coverage_checklist", []),
        "quality_gate": product.get("quality_gate"),
    }


async def run_native_question(messages, config, *, runs_dir, timeout=1800):
    """Create one native run, await its executor and close all owned resources.

    A waiting run is recorded as requiring interaction, never auto-approved.
    Only this invocation's new run is cancelled on timeout or caller cancellation.
    """
    if timeout <= 0:
        raise ValueError("evaluation timeout must be positive")
    principal = await evaluation_principal()
    service = await build_native_research_service(runs_dir=Path(runs_dir))
    run_id = None
    try:
        request = RunRequest(messages=messages, configurable=config["configurable"])
        run_id = await service.create(request, principal)
        try:
            async with asyncio.timeout(timeout):
                task = service.tasks.get(run_id)
                if task is not None:
                    await asyncio.shield(task)
        except TimeoutError:
            await service.cancel(run_id, principal.user_id)
            snapshot, _ = await service.store.load(run_id, principal.user_id)
            state = evaluation_state(snapshot)
            state["result"] = {"status": "error", "error": "evaluation_research_timeout"}
            return run_id, state
        except asyncio.CancelledError:
            await service.cancel(run_id, principal.user_id)
            raise
        snapshot, _ = await service.store.load(run_id, principal.user_id)
        from open_deep_research.agentscope_runtime.run_config import RunConfig

        frozen = RunConfig.restore(snapshot.application["configuration"])
        state = evaluation_state(snapshot)
        state["evaluation_configuration"] = {
            key: frozen.get(key) for key in config["configurable"]
            if key in frozen.compatibility_projection().get("configurable", {})
        }
        return run_id, state
    finally:
        await service.native_aclose()
