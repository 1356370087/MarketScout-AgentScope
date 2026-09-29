"""Stable evaluation contracts for research runs."""

from .contracts import EvalCase, EvalDataset, EvalTrial, GraderResult, GraderSpec
from .metrics import (
    EvaluationMetric,
    MetricStatus,
    langsmith_metric,
    normalize_evaluator_metric,
)
from .snapshot import EVALUATION_SNAPSHOT_VERSION, build_evaluation_snapshot

__all__ = [
    "EVALUATION_SNAPSHOT_VERSION",
    "JUDGE_SECURITY_PROTOCOL",
    "EvalCase",
    "EvalDataset",
    "EvalTrial",
    "EvaluationMetric",
    "GraderResult",
    "GraderSpec",
    "JudgeConfig",
    "MetricStatus",
    "build_evaluation_snapshot",
    "build_judge_model",
    "invoke_judge_structured",
    "invoke_judge_structured_sync",
    "langsmith_metric",
    "normalize_evaluator_metric",
]


def __getattr__(name):
    if name not in {
        "JUDGE_SECURITY_PROTOCOL",
        "JudgeConfig",
        "build_judge_model",
        "invoke_judge_structured",
        "invoke_judge_structured_sync",
    }:
        raise AttributeError(name)
    from . import judge

    value = getattr(judge, name)
    globals()[name] = value
    return value
