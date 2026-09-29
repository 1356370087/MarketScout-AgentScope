"""Local experiment artifacts; input digests bind every derived observation."""

import json
import os
from pathlib import Path
from xml.etree import ElementTree as ET

from .contracts import EvalDataset, EvalTrial
from .statistics import summarize


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


def load_dataset(path):
    return EvalDataset.model_validate_json(Path(path).read_text(encoding="utf-8"))


def load_trials(directory):
    return [
        EvalTrial.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted((Path(directory) / "trials").glob("*.json"))
    ]


def report(directory, trials, *, expected=None):
    directory = Path(directory)
    summary = summarize(trials, expected=expected)
    write_json(directory / "summary.json", summary)
    lines = [
        "# 本地 Agent 评估结果",
        "",
        "参考产物验证不代表 Agent 执行质量；固定环境和真实联网结果分别比较。",
        "",
        f"已记录 {len(trials)} / {summary['expected_trials']} 次试验。必需评分覆盖率：{summary['grading_coverage']}。",
        "",
        "| 任务 | 重复 | 环境 | 运行状态 | 评估判定 |",
        "| :-- | --: | :-- | :-- | :-- |",
    ]
    suite = ET.Element(
        "testsuite", name="agent-evals", tests=str(summary["expected_trials"])
    )
    lines.extend(
        f"| {t.case_id} | {t.repeat} | {t.mode} | {t.runtime_status} | {t.verdict} |"
        for t in trials
    )
    for trial in trials:
        item = ET.SubElement(
            suite, "testcase", classname=trial.case_id, name=trial.trial_id
        )
        if trial.verdict != "pass":
            element = "failure" if trial.verdict == "fail" else "error"
            ET.SubElement(item, element, message=trial.verdict).text = (
                "\n".join(
                    f"{g.grader_id}: {g.verdict}: {g.reason}"
                    for g in trial.grades
                    if g.required and g.verdict not in {"pass", "not_applicable"}
                )
                or trial.error
            )
        lines.extend(
            ["", f"### {trial.trial_id}", ""]
            + [f"- {g.grader_id}: {g.verdict} — {g.reason}" for g in trial.grades]
        )
    missing = max(0, summary["expected_trials"] - len(trials))
    for index in range(missing):
        item = ET.SubElement(
            suite, "testcase", classname="coverage", name=f"unexecuted-{index}"
        )
        ET.SubElement(item, "error", message="required_trial_not_executed")
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    ET.ElementTree(suite).write(
        directory / "junit.xml", encoding="utf-8", xml_declaration=True
    )
    return summary
