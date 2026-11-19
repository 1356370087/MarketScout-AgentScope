"""Native bounded Judges for report quality and task-specific semantic assertions."""

import json

from agentscope.message import SystemMsg, UserMsg
from pydantic import Field

from .contracts import Contract, trial_verdict
from .graders import result
from .judge import JUDGE_SECURITY_PROTOCOL
from .legacy_cli import aggregate_score, assess_quality, reconcile_judge_metrics


class SemanticGrade(Contract):
    sufficient_evidence: bool
    score: float | None = Field(default=None, ge=0, le=1)
    reason: str
    evidence_refs: list[str] = Field(default_factory=list)


async def grade_models(case, trial, session, *, repeat=0):
    specs = [g for g in case.graders if g.kind == "model"]
    metrics = []
    if any(g.check != "rubric" for g in specs):
        metrics = await session.score(
            case_id=case.id,
            sample_id=f"{trial.trial_id}:judge:{repeat}",
            inputs={"messages": [{"role": "user", "content": case.question}]},
            outputs=trial.outputs,
            reference_outputs=case.reference_outputs,
        )
        metrics = reconcile_judge_metrics(metrics)
    grades = []
    for spec in specs:
        if spec.check == "rubric":
            if (
                spec.target == "trace"
                and trial.outputs.get("evaluation_snapshot", {})
                .get("tool_trace", {})
                .get("completeness")
                != "complete"
            ):
                grades.append(result(spec, "unknown", "complete_tool_trace_required"))
                continue
            payload = {
                "question": case.question,
                "reference": case.reference_outputs,
                "outputs": trial.outputs,
                "observed_state": trial.observed_state,
            }
            with session.recovery.task(
                f"evaluation:{case.id}:{trial.trial_id}:rubric:{spec.id}:{repeat}"
            ):
                grade = await session.models.structured(
                    "quality_evaluation",
                    "",
                    SemanticGrade,
                    {},
                    messages=[
                        SystemMsg(
                            "system",
                            JUDGE_SECURITY_PROTOCOL
                            + "\n"
                            + spec.parameters["rubric"]
                            + "\nReturn sufficient_evidence=false and score=null when observations are missing. Cite supplied field paths; never invent evidence.",
                        ),
                        UserMsg("user", json.dumps(payload, ensure_ascii=False)),
                    ],
                )
            if not grade.sufficient_evidence or grade.score is None:
                grades.append(result(spec, "unknown", grade.reason))
            else:
                grades.append(
                    result(
                        spec,
                        "pass" if grade.score >= spec.threshold else "fail",
                        grade.reason,
                        score=grade.score,
                        refs=grade.evidence_refs,
                    )
                )
        elif spec.check == "quality":
            score = aggregate_score(metrics)
            assessment = assess_quality(metrics, aggregate=score)
            unscored = any(
                m["status"] in {"evaluator_error", "run_failed"} for m in metrics
            )
            if (
                score is None
                or unscored
                or any("not scored" in reason for reason in assessment["failures"])
            ):
                grades.append(
                    result(spec, "unknown", "; ".join(assessment["failures"]))
                )
            else:
                grades.append(
                    result(
                        spec,
                        "pass" if assessment["passed"] else "fail",
                        "; ".join(assessment["failures"]) or "balanced_quality_passed",
                        score=score,
                    )
                )
        else:
            metric = next((m for m in metrics if m["key"] == spec.check), None)
            if metric is None or metric["status"] != "scored":
                grades.append(
                    result(spec, "unknown", "required_metric_unavailable:" + spec.check)
                )
            else:
                score = float(metric["score"])
                grades.append(
                    result(
                        spec,
                        "pass" if score >= spec.threshold else "fail",
                        metric["comment"],
                        score=score,
                    )
                )
    trial.judge_samples.append([g.model_dump(mode="json") for g in grades])
    return grades


def apply_model_samples(trial):
    """Report Judge disagreement explicitly rather than averaging away a failure."""
    if not trial.judge_samples:
        return
    from statistics import mean

    from .contracts import GraderResult

    by_id = {}
    for sample in trial.judge_samples:
        for raw in sample:
            by_id.setdefault(raw["grader_id"], []).append(
                GraderResult.model_validate(raw)
            )
    replacements = []
    for values in by_id.values():
        base = values[0]
        if len({v.verdict for v in values}) != 1:
            base = base.model_copy(
                update={
                    "verdict": "unknown",
                    "score": None,
                    "reason": "judge_repeats_disagree",
                }
            )
        elif base.score is not None:
            base = base.model_copy(update={"score": mean(v.score for v in values)})
        replacements.append(base)
    trial.grades = [g for g in trial.grades if g.kind != "model"] + replacements
    trial.verdict = trial_verdict(trial.grades)
