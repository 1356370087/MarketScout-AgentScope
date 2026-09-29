"""Canonical metric states and adapters for evaluation backends."""

from __future__ import annotations

import math
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator


class MetricStatus(str, Enum):
    """The lifecycle state of one evaluation dimension."""

    SCORED = "scored"
    NOT_SCORED = "not_scored"
    NOT_APPLICABLE = "not_applicable"
    RUN_FAILED = "run_failed"
    EVALUATOR_ERROR = "evaluator_error"


class EvaluationMetric(BaseModel):
    """Backend-independent representation of one evaluation result."""

    model_config = ConfigDict(extra="forbid")

    key: str
    score: bool | int | float | None
    comment: str = ""
    status: MetricStatus
    evaluator: str | None = None

    @model_validator(mode="after")
    def validate_score_status(self) -> EvaluationMetric:
        """Require a score only for successfully scored dimensions."""
        if self.status == MetricStatus.SCORED and self.score is None:
            raise ValueError("scored metrics require a numeric or boolean score")
        if self.status != MetricStatus.SCORED and self.score is not None:
            raise ValueError("unscored metrics must not carry a score")
        if self.score is not None and not math.isfinite(float(self.score)):
            raise ValueError("scores must be finite")
        return self


def primary_metrics(metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Choose the canonical evidence inventory once, regardless of row order."""
    result: dict[str, dict[str, Any]] = {}
    evidence = {
        "groundedness_score",
        "citation_accuracy_score",
        "source_authority_score",
        "factual_accuracy_score",
    }
    for row in metrics:
        key = row["key"]
        if key.startswith("diagnostic."):
            continue
        old = result.get(key)
        if old is None or (
            key in evidence and row.get("evaluator") == "evidence_integrity"
        ):
            result[key] = row
        elif old != row and not (
            key in evidence and old.get("evaluator") == "evidence_integrity"
        ):
            raise ValueError("ambiguous_primary_metric:" + key)
    return list(result.values())


def langsmith_metric(
    key: str,
    *,
    status: MetricStatus,
    score: bool | float | None = None,
    comment: str = "",
) -> dict[str, Any]:
    """Adapt a canonical metric to LangSmith's evaluator result contract."""
    metric = EvaluationMetric(
        key=key,
        score=score,
        comment=comment,
        status=status,
    )
    return {
        "key": metric.key,
        "score": metric.score,
        "comment": metric.comment,
        "metadata": {"metric_status": metric.status.value},
    }


def normalize_evaluator_metric(
    evaluator_name: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Adapt LangSmith-style output to the local evaluation artifact contract."""
    metadata = result.get("metadata")
    raw_status = metadata.get("metric_status") if isinstance(metadata, dict) else None
    score = result.get("score")
    comment = str(result.get("comment", ""))
    if raw_status is None:
        if comment.startswith("Run failed:"):
            status = MetricStatus.RUN_FAILED
            score = None
        elif comment.startswith("Not scored:"):
            status = MetricStatus.NOT_SCORED
            score = None
        else:
            status = (
                MetricStatus.SCORED if score is not None else MetricStatus.NOT_SCORED
            )
    else:
        status = MetricStatus(str(raw_status))
    metric = EvaluationMetric(
        evaluator=evaluator_name,
        key=str(result["key"]),
        score=score,
        comment=comment,
        status=status,
    )
    return metric.model_dump(mode="json", exclude_none=False)
