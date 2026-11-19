"""Strict paired metric comparison; unavailable scores never become zero."""

import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict

from .metrics import EvaluationMetric, MetricStatus


def fingerprint(value):
    """Hash public frozen evaluation inputs, excluding credentials by contract."""
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def compare_samples(baseline, candidate):
    """Compare matching case/repeat/metric rows and expose missing pair coverage."""

    def index(rows):
        result = {}
        for row in rows:
            key = (
                row["case_id"],
                row["repeat"],
                row["metric"]["evaluator"],
                row["metric"]["key"],
            )
            if key in result:
                raise ValueError("duplicate_evaluation_sample")
            metric = EvaluationMetric.model_validate(row["metric"])
            if metric.score is not None and not math.isfinite(float(metric.score)):
                raise ValueError("nonfinite_evaluation_score")
            result[key] = (row, metric)
        return result

    left, right = index(baseline), index(candidate)
    if not left or left.keys() != right.keys():
        raise ValueError("evaluation_pair_set_mismatch")
    deltas = defaultdict(list)
    baseline_scores, candidate_scores = defaultdict(list), defaultdict(list)
    statuses = defaultdict(Counter)
    for key in sorted(left):
        old, old_metric = left[key]
        new, new_metric = right[key]
        for field in ("dataset_sha256", "judge_sha256", "rubric_sha256"):
            if not old.get(field) or old[field] != new.get(field):
                raise ValueError("evaluation_pair_fingerprint_mismatch:" + field)
        group = key[2] + "/" + key[3]
        statuses[group][old_metric.status.value + "/" + new_metric.status.value] += 1
        if old_metric.status == new_metric.status == MetricStatus.SCORED:
            deltas[group].append(float(new_metric.score) - float(old_metric.score))
            baseline_scores[group].append(float(old_metric.score))
            candidate_scores[group].append(float(new_metric.score))
    return {
        group: {
            "pairs": sum(counts.values()),
            "scored_pairs": len(deltas[group]),
            "status_pairs": dict(counts),
            "mean_delta": statistics.mean(deltas[group]) if deltas[group] else None,
            "baseline_mean": statistics.mean(baseline_scores[group])
            if deltas[group]
            else None,
            "candidate_mean": statistics.mean(candidate_scores[group])
            if deltas[group]
            else None,
            "baseline_sample_stdev": statistics.stdev(baseline_scores[group])
            if len(deltas[group]) > 1
            else None,
            "candidate_sample_stdev": statistics.stdev(candidate_scores[group])
            if len(deltas[group]) > 1
            else None,
            "sample_stdev_delta": statistics.stdev(deltas[group])
            if len(deltas[group]) > 1
            else None,
            "min_delta": min(deltas[group]) if deltas[group] else None,
            "max_delta": max(deltas[group]) if deltas[group] else None,
        }
        for group, counts in statuses.items()
    }
