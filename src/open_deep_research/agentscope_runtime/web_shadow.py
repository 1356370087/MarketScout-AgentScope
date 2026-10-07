"""Deterministically sampled, budgeted shadow evidence with no repeated search."""

import asyncio
import hashlib
import time

from open_deep_research.agentscope_runtime.search_providers import (
    preserve_control_error,
)
from open_deep_research.agentscope_runtime.web_progress import shadow_model_usage
from open_deep_research.budgets import BudgetExhausted
from open_deep_research.configuration import Configuration
from open_deep_research.sandbox.egress_context import (
    egress_probe_only,
)


def shadow_selected(operation_id: str, rate: float) -> bool:
    """Stable sampling survives retries and recovery without mutable random state."""
    value = (
        int.from_bytes(hashlib.sha256(operation_id.encode()).digest()[:8], "big")
        / 2**64
    )
    return value < rate


async def evaluate_shadow(
    config,
    request,
    batch,
    factory,
    resources,
    ledger,
    *,
    browser_tools=(),
    progress=None,
):
    """Run optional evidence acquisition in the parent tool's durable operation."""
    from open_deep_research.agentscope_runtime.web_tools import (
        create_web_pipeline,
        run_web_pipeline,
    )

    cfg = Configuration.from_runnable_config(config)
    operation_id = str(config.get("metadata", {}).get("tool_operation_id") or "")
    if cfg.web_pipeline_mode != "shadow" or not shadow_selected(
        operation_id, cfg.web_pipeline_shadow_sample_rate
    ):
        return {"status": "not_sampled"}, 0
    if not batch.candidates:
        return {"status": "skipped", "reason": "no_candidates"}, 0
    started = time.monotonic()
    fetches = 0
    tally = {"physical_fetches": 0}
    refunded = 0
    usage = {"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    async def approve(items, iteration):
        from open_deep_research.agentscope_runtime.web_tools import (
            _approve_candidate_batch,
        )

        return await _approve_candidate_batch(
            items,
            iteration,
            config,
            str(config.get("metadata", {}).get("run_id", "default")),
        )

    async def admitted_batch(current_request):
        admission = await approve(batch.candidates, current_request.iteration)
        blocked = set(admission.pending_domains + admission.denied_domains)
        errors = list(batch.errors)
        if admission.pending_domains:
            errors.append("shadow_approval_required")
        if admission.denied_domains:
            errors.append("shadow_source_denied")
        return batch.model_copy(
            update={
                "candidates": [c for c in batch.candidates if c.domain not in blocked],
                "errors": errors,
            }
        )

    token = egress_probe_only.set(True)
    usage_token = shadow_model_usage.set(usage)
    try:
        pipeline = create_web_pipeline(
            config,
            factory,
            resources,
            browser_tools=browser_tools,
            batch=batch,
            progress=progress,
            top_k=cfg.web_shadow_fetch_top_k,
            approve=approve,
        )
        # Preflight precedes model ranking, so unknown domains do not trigger
        # classifier/approval/model work just to produce a shadow comparison.
        pipeline.search = admitted_batch
        async with asyncio.timeout(
            min(cfg.web_shadow_timeout_seconds, cfg.research_tool_call_timeout_seconds)
        ):
            result, metadata = await run_web_pipeline(
                pipeline, request, ledger, config, tally=tally
            )
        fetches = metadata["physical_fetches"]
        refunded = metadata["transport_failed_fetches"]
        pending = (
            result.approval_batch and result.approval_batch.pending_domains
        ) or "shadow_approval_required" in result.errors
        reason = (
            "approval_required"
            if pending and not result.documents
            else result.gap_analysis.reason
        )
        diagnostics = {
            "status": "completed" if result.documents else "skipped",
            "reason": reason,
            "raw_candidate_count": batch.raw_candidate_count,
            "candidate_count": len(batch.candidates),
            "admitted_candidate_count": len(result.candidates),
            "extra_search_calls": 0,
            "deduplicated_count": max(
                0, batch.raw_candidate_count - len(batch.candidates)
            ),
            "selected_count": sum(item.selected for item in result.ranked_candidates),
            "fetched_count": len(result.documents),
            "fetch_calls": fetches,
            "fetch_success_rate": len(result.documents) / fetches if fetches else 0.0,
            "evidence_count": len(result.evidence),
            "source_overlap": len(
                {e.source_url for e in result.evidence}
                & {c.canonical_url for c in batch.candidates}
            ),
            "error_codes": [error.rsplit(": ", 1)[-1] for error in result.errors],
        }
    except TimeoutError:
        diagnostics = {"status": "timed_out", "reason": "shadow_timeout"}
    except BudgetExhausted:
        # The budget authority rejected the optional work before dispatch;
        # keep the already-produced primary summary and record the refusal.
        diagnostics = {"status": "skipped", "reason": "budget_exhausted"}
    except Exception as exc:  # noqa: BLE001 - normalize external failures after preserving runtime control
        preserve_control_error(exc)
        diagnostics = {"status": "failed", "reason": type(exc).__name__}
    finally:
        egress_probe_only.reset(token)
        shadow_model_usage.reset(usage_token)
    fetches = tally["physical_fetches"]
    diagnostics["fetch_calls"] = fetches
    diagnostics["charged_fetch_calls"] = max(0, fetches - refunded)
    diagnostics["model_usage"] = usage
    diagnostics["duration_ms"] = round((time.monotonic() - started) * 1000)
    if progress:
        await progress(
            "shadow_completed"
            if diagnostics["status"] == "completed"
            else "shadow_skipped",
            metrics=diagnostics,
            reason=diagnostics.get("reason", ""),
        )
    return diagnostics, fetches
