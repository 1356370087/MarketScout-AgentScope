"""Blinded local human review and explicit, source-bound calibration labels."""

import hashlib
import json
import random
import statistics
from collections import defaultdict
from pathlib import Path

from pydantic import Field

from .artifacts import load_trials, report, write_json
from .contracts import Contract, GraderResult, trial_verdict


class Annotation(Contract):
    sample_id: str
    reviewer_id: str = Field(min_length=1)
    criterion: str
    verdict: str = Field(pattern=r"^(pass|fail|unknown)$")
    score: float | None = Field(default=None, ge=0, le=1)
    reason: str = Field(min_length=1)
    adjudication: bool = False


CRITERIA = {
    "decision_value": "结论是否帮助用户作出题目要求的判断；1=支持决策，0.5=帮助有限，0=没有帮助。",
    "actionability": "建议是否具体、可执行且适合问题边界；1=可操作，0.5=部分可操作，0=空泛。",
    "uncertainty": "是否准确表达证据缺口、冲突和不确定性；1=准确，0.5=部分遗漏，0=误导。",
    "quality": "结合任务与来源核对正确性、覆盖和引用；1=合格，0.5=需明显修订，0=不可用。",
}


def export_review(source, destination, *, limit=20, seed=0):
    source, destination = Path(source).resolve(), Path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("review_destination_must_be_empty")
    trials = load_trials(source)
    # Round robin across outcomes prevents a review set consisting only of successes.
    groups = defaultdict(list)
    for trial in trials:
        groups[trial.verdict].append(trial)
    rng = random.Random(seed)
    for rows in groups.values():
        rng.shuffle(rows)
    selected = []
    while any(groups.values()) and len(selected) < limit:
        for key in sorted(groups):
            if groups[key] and len(selected) < limit:
                selected.append(groups[key].pop())
    rng.shuffle(selected)
    dataset = json.loads((source / "dataset.json").read_text(encoding="utf-8"))
    cases = {c["id"]: c for c in dataset["cases"]}
    private, labels = {}, []
    destination.mkdir(parents=True, exist_ok=True)
    for index, trial in enumerate(selected, 1):
        sample = f"sample-{index:03d}"
        path = source / "trials" / (trial.trial_id + ".json")
        private[sample] = {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "case_id": trial.case_id,
            "trial_id": trial.trial_id,
        }
        case = cases[trial.case_id]
        snapshot = trial.outputs.get("evaluation_snapshot", {})
        # Never include experiment/model identity, automated grades, or SQL run ids.
        calls = snapshot.get("tool_trace", {})
        public_calls = [
            {
                k: call.get(k)
                for k in (
                    "name",
                    "args",
                    "state",
                    "error",
                    "content_preview",
                    "content_truncated",
                )
            }
            for call in calls.get("supervisor_tool_calls", [])
            + calls.get("researcher_tool_calls", [])
        ]
        text = [
            f"# {sample}",
            "",
            "## 任务",
            "",
            case["question"],
            "",
            "## 待审阅报告",
            "",
            trial.outputs.get("final_report", "（没有报告）"),
            "",
            "## 证据与执行记录",
            "",
            "```json",
            json.dumps(
                {
                    "evidence": snapshot.get("evidence_registry", []),
                    "calls": public_calls,
                    "observed_state": trial.observed_state,
                },
                ensure_ascii=False,
                indent=2,
            ),
            "```",
            "",
            "## 评分标准",
            "",
        ]
        human = [g for g in case["graders"] if g["kind"] == "human"]
        criteria = {
            **CRITERIA,
            **{g["id"]: g["parameters"].get("rubric", g["check"]) for g in human},
        }
        private[sample]["criteria"] = {key: 0.75 for key in criteria}
        private[sample]["criteria"].update({g["id"]: g["threshold"] for g in human})
        for criterion, rubric in criteria.items():
            text.append(
                f"- {criterion}：{rubric} 合格阈值：{private[sample]['criteria'][criterion]}。"
            )
            labels.append(
                {
                    "sample_id": sample,
                    "reviewer_id": "",
                    "criterion": criterion,
                    "verdict": "unknown",
                    "score": None,
                    "reason": "",
                    "adjudication": False,
                }
            )
        (destination / (sample + ".md")).write_text("\n".join(text), encoding="utf-8")
    write_json(destination / "private-index.json", private)
    (destination / "annotations-template.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in labels) + "\n",
        encoding="utf-8",
    )
    (destination / "README.md").write_text(
        "# 人工审阅\n\n逐份阅读 sample 文件，填写标注模板；隐藏 private-index.json，避免获知实验身份。"
        "分数达到 0.75 视为合格，证据不足填写 unknown 和理由。保留每位评分者的独立文件。"
        "第二位评分者存在时统计一致性；有争议时由人工追加 adjudication=true 的明确裁决。\n",
        encoding="utf-8",
    )
    return {
        "samples": len(selected),
        "requested": limit,
        "calibration": "pending_human_labels",
    }


def import_review(review_dir, annotations_path, destination):
    review_dir, destination = Path(review_dir), Path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("derived_review_destination_must_be_empty")
    index = json.loads((review_dir / "private-index.json").read_text(encoding="utf-8"))
    labels = [
        Annotation.model_validate_json(line)
        for line in Path(annotations_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    seen, grouped, trials = set(), defaultdict(list), {}
    for label in labels:
        key = (label.sample_id, label.reviewer_id, label.criterion, label.adjudication)
        if key in seen:
            raise ValueError("duplicate_human_annotation")
        seen.add(key)
        ref = index[label.sample_id]
        if label.criterion not in ref["criteria"]:
            raise ValueError("unknown_review_criterion")
        path = Path(ref["path"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != ref["sha256"]:
            raise ValueError("review_source_changed")
        if (
            label.verdict == "unknown"
            and label.score is not None
            or label.verdict != "unknown"
            and label.score is None
        ):
            raise ValueError("human_verdict_score_mismatch")
        if label.score is not None and (
            label.score >= ref["criteria"][label.criterion]
        ) != (label.verdict == "pass"):
            raise ValueError("human_score_threshold_mismatch")
        grouped[(label.sample_id, label.criterion)].append(label)
        from .contracts import EvalTrial

        trials.setdefault(
            label.sample_id,
            EvalTrial.model_validate_json(path.read_text(encoding="utf-8")),
        )
    resolved, conflicts, model_pairs, human_pairs = [], [], [], []
    for (sample, criterion), rows in grouped.items():
        reviewers = [r for r in rows if not r.adjudication and r.verdict != "unknown"]
        for i, left in enumerate(reviewers):
            for right in reviewers[i + 1 :]:
                human_pairs.append(left.verdict == right.verdict)
        adjudications = [r for r in rows if r.adjudication]
        if len(adjudications) > 1:
            raise ValueError("multiple_adjudications")
        selected = adjudications or rows
        verdicts = {r.verdict for r in selected}
        consensus = len(verdicts) == 1 and "unknown" not in verdicts
        verdict = next(iter(verdicts)) if consensus else "unknown"
        score = statistics.mean(r.score for r in selected) if consensus else None
        if not consensus:
            conflicts.append({"sample_id": sample, "criterion": criterion})
        trial = trials[sample]
        old = next(
            (g for g in trial.grades if g.grader_id == criterion and g.kind == "human"),
            None,
        )
        grade = GraderResult(
            grader_id=criterion,
            kind="human",
            target="quality",
            required=old.required if old else False,
            verdict=verdict,
            score=score,
            reason=" | ".join(r.reason for r in selected),
        )
        trial.grades = [
            g
            for g in trial.grades
            if not (g.grader_id == criterion and g.kind == "human")
        ] + [grade]
        trial.verdict = trial_verdict(trial.grades)
        if criterion == "quality" and consensus:
            model = next(
                (
                    g
                    for g in trial.grades
                    if g.kind == "model"
                    and g.target == "quality"
                    and g.score is not None
                ),
                None,
            )
            if model:
                model_pairs.append(
                    {
                        "agreement": model.verdict == verdict,
                        "absolute_error": abs(model.score - score),
                    }
                )
        resolved.append(
            {
                "sample_id": sample,
                "criterion": criterion,
                "verdict": verdict,
                "score": score,
            }
        )
    source_dirs = {Path(ref["path"]).parent.parent for ref in index.values()}
    if len(source_dirs) != 1:
        raise ValueError("review_requires_one_source_experiment")
    source = next(iter(source_dirs))
    all_trials = {t.trial_id: t for t in load_trials(source)}
    all_trials.update({t.trial_id: t for t in trials.values()})
    for trial in all_trials.values():
        write_json(
            destination / "trials" / (trial.trial_id + ".json"),
            trial.model_dump(mode="json"),
        )
    write_json(
        destination / "dataset.json",
        json.loads((source / "dataset.json").read_text(encoding="utf-8")),
    )
    source_summary = source / "summary.json"
    expected = (
        json.loads(source_summary.read_text(encoding="utf-8"))["expected_trials"]
        if source_summary.exists()
        else len(all_trials)
    )
    report(destination, list(all_trials.values()), expected=expected)
    summary = {
        "annotations": len(labels),
        "resolved": resolved,
        "disagreements": conflicts,
        "human_pair_count": len(human_pairs),
        "inter_rater_agreement": statistics.mean(human_pairs) if human_pairs else None,
        "model_human_pairs": len(model_pairs),
        "model_human_agreement": statistics.mean(p["agreement"] for p in model_pairs)
        if model_pairs
        else None,
        "model_mean_absolute_error": statistics.mean(
            p["absolute_error"] for p in model_pairs
        )
        if model_pairs
        else None,
        "status": "labels_recorded" if labels else "pending_human_labels",
    }
    write_json(destination / "calibration.json", summary)
    write_json(
        destination / "annotations.json", [r.model_dump(mode="json") for r in labels]
    )
    return summary
