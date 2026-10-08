"""Run existing evaluator rubrics through governed native model operations."""

import asyncio
from contextlib import nullcontext

from agentscope.message import SystemMsg, UserMsg

from .judge import native_judge
from .metrics import EvaluationMetric, MetricStatus, normalize_evaluator_metric

METRICS = (
    "overall_quality",
    "relevance",
    "structure",
    "correctness",
    "evidence_integrity",
    "groundedness",
    "completeness",
    "citation_accuracy",
    "tool_efficiency",
    "execution_compliance",
)
DIAGNOSTICS = {"groundedness", "citation_accuracy"}


class EvaluationInterrupted(BaseException):
    """Carry control failures through evaluator retry/error normalization."""

    def __init__(self, error):
        self.error = error
        super().__init__(type(error).__name__)


class EvaluationInputBudgetExceeded(ValueError):
    """An intact archived input cannot fit the frozen Judge context."""


def complete_metric_categories(metrics):
    """Represent non-applicable legacy empty lists without fabricating scores."""
    result = list(metrics)
    present = {row["evaluator"] for row in metrics}
    for metric in METRICS:
        if metric not in present:
            result.append(
                EvaluationMetric(
                    evaluator=metric,
                    key=("diagnostic." if metric in DIAGNOSTICS else "")
                    + metric
                    + "_score",
                    score=None,
                    status=MetricStatus.NOT_APPLICABLE
                    if metric in DIAGNOSTICS
                    else MetricStatus.NOT_SCORED,
                    comment="Not scored: evaluator has no applicable requirements or traces.",
                ).model_dump(mode="json")
            )
    return result


async def evaluate_output(
    models, *, case_id, sample_id, inputs, outputs, reference_outputs=None,
    max_input_tokens=None,
):
    """Evaluate an archived output without rerunning or resuming its research.

    The caller owns the evaluation RecoverySession, model budget and frozen
    model configuration. Each sample uses a distinct journal task scope.
    """
    from . import evaluators

    loop = asyncio.get_running_loop()
    recovery = models.recovery
    results = []

    for metric in METRICS:
        if metric in DIAGNOSTICS:
            continue  # The canonical inventory supplies these primary scores.

        async def invoke(schema, messages, operation, metric=metric):
            from open_deep_research.agentscope_runtime.recovery import ApprovalPending
            from open_deep_research.agentscope_runtime.recovery_store import (
                FenceLost,
                RecoveryConflict,
                UnknownOperation,
            )
            from open_deep_research.budgets import BudgetExhausted, DeadlineExceeded

            task_id = f"evaluation:{case_id}:{sample_id}:{metric}"
            if max_input_tokens is not None:
                # The report runtime uses the same conservative UTF-8 bound.
                size = sum(len(m["content"].encode("utf8")) + 16 for m in messages)
                if size > max_input_tokens:
                    raise EvaluationInputBudgetExceeded("judge_input_exceeds_frozen_context")
            native = []
            for message in messages:
                if message["role"] not in {"system", "user"}:
                    raise ValueError("unsupported_evaluation_message_role")
                cls = SystemMsg if message["role"] == "system" else UserMsg
                native.append(cls(message["role"], message["content"]))
            try:
                with recovery.task(task_id) if recovery else nullcontext():
                    return await models.structured(
                        "quality_evaluation",
                        "",
                        schema,
                        {"task_id": task_id},
                        messages=native,
                    )
            except (
                ApprovalPending,
                FenceLost,
                RecoveryConflict,
                UnknownOperation,
                BudgetExhausted,
                DeadlineExceeded,
            ) as error:
                raise EvaluationInterrupted(error) from error
            except Exception as error:
                if recovery and recovery.problem is not None:
                    raise EvaluationInterrupted(error) from error
                raise

        pending = []

        def adapter(schema, messages, *, operation, pending=pending):
            from concurrent.futures import CancelledError

            future = asyncio.run_coroutine_threadsafe(
                invoke(schema, messages, operation), loop
            )
            pending.append(future)
            try:
                return future.result()
            except CancelledError as error:
                raise EvaluationInterrupted(asyncio.CancelledError()) from error

        def evaluate(metric=metric):
            token = native_judge.set(adapter)
            try:
                evaluator = getattr(evaluators, "eval_" + metric)
                if metric == "correctness":
                    return evaluator(inputs, outputs, reference_outputs or {})
                return evaluator(inputs, outputs)
            finally:
                native_judge.reset(token)

        try:
            result = await asyncio.to_thread(evaluate)
        except EvaluationInterrupted as error:
            raise error.error from error
        except asyncio.CancelledError:
            for future in pending:
                future.cancel()
            raise
        except Exception as error:  # noqa: BLE001 -- Persist a sanitized evaluator failure.
            results.append(
                EvaluationMetric(
                    evaluator=metric,
                    key=metric + "_score",
                    score=None,
                    status=MetricStatus.EVALUATOR_ERROR,
                    comment="Evaluator failed: " + type(error).__name__,
                ).model_dump(mode="json")
            )
            continue
        rows = result if isinstance(result, list) else [result]
        results.extend(normalize_evaluator_metric(metric, row) for row in rows)
    return complete_metric_categories(results)
