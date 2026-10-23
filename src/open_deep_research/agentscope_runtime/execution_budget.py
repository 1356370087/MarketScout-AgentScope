"""Shared v21 research admission and resumable report time authorization."""

import math
from uuid import uuid4

from open_deep_research.budgets import BudgetDimension, BudgetExhausted, DeadlineExceeded
from open_deep_research.configuration import Configuration

APPROVAL_STAGE = "report_time_extension_approval"
MAX_REQUESTS = 2


class ReportTimeApprovalPending(RuntimeError):
    """A report checkpoint needs an explicit user decision before another call."""

    def __init__(self, pending):
        super().__init__("report_time_extension_required")
        self.pending = pending


def enabled(config):
    return config.get("metadata", {}).get("run_config_schema_version", 18) >= 21


def report_steps(cfg, progress=None, *, outline=False):
    """Remaining bounded report work; actual steps retire their own estimates."""
    progress = progress or {}
    if progress.get("review_passed"):
        return []
    done = list(progress.get("completed_calls", {}).values())
    revisions = sum(row["span"] == "lead.report_revision" for row in done)
    reviews = sum(row["span"] == "lead.report_review" for row in done)
    from open_deep_research.report.profiles import AssemblyMode, get_profile
    sectioned = get_profile(cfg.report_type).assembly == AssemblyMode.SECTIONED
    expected_writes = progress.get("expected_writing_calls", 8 if sectioned else 1)
    written = sum(row["step"] == "writing" for row in done)
    steps = []
    if (outline or sectioned) and not any(row["step"] == "outline" for row in done):
        steps.append(("outline", "final_report", 60))
    steps.extend(("writing", "final_report", 120) for _ in range(max(0, expected_writes - written)))
    if not progress.get("draft_checked") and not any(row["step"] == "citation" for row in done):
        steps.append(("citation", "final_report", 60))
    if cfg.report_review_enabled:
        if reviews <= revisions:
            steps.append(("review", "report_review", 60))
        for _ in range(max(0, cfg.report_review_max_revisions - revisions)):
            steps.extend([("revision", "report_revisor", 120), ("review", "report_review", 60)])
    return steps


def call_step(role, span):
    if "outline" in span:
        return "outline"
    if "citation" in span:
        return "citation"
    if role == "report_revisor":
        return "revision"
    if role in {"report_review", "quality_evaluation"}:
        return "review"
    return "writing"


def estimate_seconds(cfg, samples=(), progress=None, *, outline=False, current=None):
    steps = report_steps(cfg, progress, outline=outline)
    if current and not any(step[0] == current[0] for step in steps):
        steps.insert(0, current)
    total = 0.0
    for _, role, cold in steps:
        model = role_model(cfg, role)
        values = [row["seconds"] for row in samples
                  if row["role"] == role and row["model"] == model and row["seconds"] >= 0][-20:]
        duration = sorted(values)[math.ceil(len(values) * .9) - 1] if len(values) >= 3 else cold
        total += min(duration, cfg.model_call_timeout_seconds)
    return math.ceil(total * 1.25 + 30)


def role_model(cfg, role):
    if role == "report_revisor":
        return cfg.final_report_model
    if role == "report_review":
        return cfg.report_review_model or cfg.quality_evaluation_model or cfg.research_model
    return getattr(cfg, role + "_model", None) or cfg.research_model


async def timing_state(models, config, *, include_samples=True):
    recovery = getattr(models, "recovery", None)
    if recovery is None:
        return None
    return await recovery.store.execution_timing(recovery.lease.run_id, recovery.lease.user_id,
                                                Configuration.from_runnable_config(config), include_samples=include_samples)


async def research_stop_reason(models, config):
    """A persistent closing reason cannot be undone by faster later estimates."""
    if not enabled(config):
        return None
    recovery = getattr(models, "recovery", None)
    if recovery is None:
        return None
    # Before report generation there are no report-role calls to sample. Member
    # polling must not reload the entire model event history every half second.
    timing = await timing_state(models, config, include_samples=False)
    if timing["closing_reason"]:
        return timing["closing_reason"]
    cfg = Configuration.from_runnable_config(config)
    report = estimate_seconds(cfg, timing["samples"], outline=cfg.enable_human_in_loop)
    floor = (cfg.run_deadline_seconds or 0) * cfg.report_time_reserve_ratio
    reserve = max(report, floor, timing["inflight_seconds"] + 30)
    remaining = timing["remaining_seconds"]
    reason = "report_time_reserved" if remaining is not None and remaining <= reserve else None
    fetches = timing["budget"]["limits"].get("fetch_calls")
    if reason is None and fetches is not None and (
        timing["budget"]["used"].get("fetch_calls", 0)
        + timing["budget"]["reserved"].get("fetch_calls", 0) >= fetches
    ):
        # 缓存和已交付工件仍可读；停止规划新的外部补研。
        reason = "fetch_budget_exhausted"
    if reason:
        reason = await recovery.store.close_research(recovery.lease, reason)
        recovery.snapshot.application.setdefault("execution_budget", {})["research_closing_reason"] = reason
    return reason


async def before_report_call(models, snapshot, config, *, role, span, checkpoint, operation_key):
    """Park before dispatch, never after a model result becomes uncertain."""
    if not enabled(config) or getattr(models, "recovery", None) is None:
        return
    recovery = models.recovery
    if recovery.problem:
        raise recovery.problem
    record = await recovery.store.operation_record(recovery.lease, operation_key) if operation_key else None
    if record is not None:
        # Journal replay/unknown-result isolation has priority over time requests.
        return
    cfg = Configuration.from_runnable_config(config)
    timing = await timing_state(models, config)
    remaining = timing["remaining_seconds"]
    if remaining is None:
        return
    state = snapshot.application.setdefault("execution_budget", {})
    progress = state.setdefault("report", {})
    current = (call_step(role, span), role, 120 if role in {"final_report", "report_revisor"} else 60)
    estimate = estimate_seconds(cfg, timing["samples"], progress, current=current)
    state["estimate"] = {"remaining_seconds": remaining, "estimated_report_seconds": estimate,
                         "checkpoint": checkpoint}
    if remaining >= estimate:
        return
    history = state.get("extension_history", [])
    if state.get("extension_denied"):
        raise DeadlineExceeded("report_time_extension_denied")
    if checkpoint in state.get("authorized_checkpoints", []):
        # A shorter user-selected grant must not immediately prompt again.
        if remaining <= 0:
            raise DeadlineExceeded("report time exhausted after authorization")
        return
    if len(history) >= MAX_REQUESTS:
        raise DeadlineExceeded("report_time_extension_limit")
    budget = timing["budget"]
    for dimension, maximum in budget["limits"].items():
        if dimension != "fetch_calls" and budget["used"].get(dimension, 0) + budget["reserved"].get(dimension, 0) >= maximum:
            raise BudgetExhausted(BudgetDimension(dimension))
    from .research_pipeline import PendingDecision

    suggested = min(1800, max(300, math.ceil((estimate - remaining) / 60) * 60))
    pending = PendingDecision(
        id=uuid4().hex, stage=APPROVAL_STAGE,
        question=f"报告预计还需约 {math.ceil(estimate / 60)} 分钟，剩余执行时间约 "
                 f"{math.ceil(remaining / 60)} 分钟。建议延长 {suggested // 60} 分钟，"
                 "仅用于写作、引用检查和复核，不增加费用或抓取额度。等待决定期间暂停执行和计时。",
        payload={"version": len(history) + 1, "checkpoint": checkpoint, "report_step": current[0],
                 "remaining_seconds": remaining, "estimated_report_seconds": estimate,
                 "suggested_extension_seconds": suggested, "extension_count": len(history),
                 "max_requests": MAX_REQUESTS, "completed_steps": sorted({r["step"] for r in progress.get("completed_calls", {}).values()}),
                 "allowed_actions": ["approve", "deny", "cancel"]},
    )
    raise ReportTimeApprovalPending(pending)


def validate_extension(pending, action, expected_version, seconds=None):
    from .recovery_store import RecoveryConflict

    payload = pending["payload"]
    if expected_version != payload["version"]:
        raise RecoveryConflict("report_time_extension_version_changed")
    if action not in {"approve", "deny", "cancel"}:
        raise ValueError("invalid report time decision")
    if seconds is not None and (action != "approve" or isinstance(seconds, bool)
                                or not isinstance(seconds, int) or not 300 <= seconds <= 1800 or seconds % 60):
        raise ValueError("extension_seconds must be whole minutes between 300 and 1800")
    return (seconds if seconds is not None else payload["suggested_extension_seconds"]) if action == "approve" else 0
