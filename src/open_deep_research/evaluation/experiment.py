"""Frozen artifact pairs scored with the same native Judge and rubric."""

import hashlib
import json
from pathlib import Path

from .paired import compare_samples, fingerprint
from .session import native_judge_session


def output_state(artifact):
    """Read local evaluation products without opening any research engine."""
    state = dict(artifact)
    state["result"] = artifact.get("run_result", artifact.get("result", {}))
    if artifact.get("status") in {"failed", "cancelled"}:
        state["result"] = {"status": "error", "error": "source_run_failed"}
    state["evaluation_metadata"] = artifact.get("configuration", {})
    return state


def load_pairs(path):
    path = Path(path)
    dataset = json.loads(path.read_text(encoding="utf-8"))
    if dataset.get("schema_version") != 1 or not dataset.get("cases"):
        raise ValueError("invalid_paired_dataset")
    cases, ids = [], set()
    for case in dataset["cases"]:
        if case["id"] in ids:
            raise ValueError("duplicate_case_id")
        ids.add(case["id"])
        if case.get("kind") == "knowledge":
            refs = case.get("corpus_refs")
            if (
                not isinstance(refs, list)
                or not refs
                or any(
                    not isinstance(ref, dict)
                    or any(
                        not isinstance(ref.get(key), str)
                        or not ref[key].strip()
                        or ref[key].startswith("TBD")
                        for key in ("artifact_version", "generation", "unit")
                    )
                    for ref in refs
                )
            ):
                raise ValueError("knowledge_corpus_versions_required")
        sides, hashes = {}, {}
        for side in ("baseline", "candidate"):
            content = (path.parent / case[side]).read_bytes()
            hashes[side] = hashlib.sha256(content).hexdigest()
            if case.get(side + "_sha256") != hashes[side]:
                raise ValueError("paired_artifact_hash_mismatch:" + side)
            artifact = json.loads(content)
            if artifact.get("question") != case["question"]:
                raise ValueError("paired_question_mismatch")
            sides[side] = output_state(artifact)
        cases.append({**case, "outputs": sides, "artifact_hashes": hashes})
    return dataset, cases


async def evaluate_pairs(dataset_path, output_dir, *, repeats=2):
    if repeats < 2:
        raise ValueError("paired_evaluation_requires_two_repeats")
    dataset, cases = load_pairs(dataset_path)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    frozen = {"dataset": dataset, "repeats": repeats}
    frozen_path = directory / "dataset.json"
    if frozen_path.exists():
        if json.loads(frozen_path.read_text(encoding="utf-8")) != frozen:
            raise ValueError("frozen_dataset_changed")
    else:
        with frozen_path.open("x", encoding="utf-8") as stream:
            json.dump(frozen, stream, ensure_ascii=False, indent=2)
    rows = {"baseline": [], "candidate": []}
    async with native_judge_session(directory / "judge") as judge:
        for repeat in range(repeats):
            for case in cases:
                # Alternate order to reduce ordering bias; never alter the rubric.
                for side in (
                    ("baseline", "candidate")
                    if repeat % 2 == 0
                    else ("candidate", "baseline")
                ):
                    metrics = await judge.score(
                        case_id=case["id"],
                        sample_id=f"{side}:{repeat}",
                        inputs={
                            "messages": [{"role": "user", "content": case["question"]}]
                        },
                        outputs=case["outputs"][side],
                        reference_outputs=case.get("reference_outputs", {}),
                    )
                    rows[side].extend(
                        {
                            "case_id": case["id"],
                            "repeat": repeat,
                            "metric": metric,
                            "dataset_sha256": fingerprint(dataset),
                            "judge_sha256": fingerprint(
                                [
                                    judge.manifest["judge_sha256"],
                                    judge.manifest["evaluation_date"],
                                ]
                            ),
                            "rubric_sha256": judge.manifest["rubric_sha256"],
                            "artifact_sha256": case["artifact_hashes"][side],
                        }
                        for metric in metrics
                    )
                    (directory / "samples.json").write_text(
                        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
        comparison = compare_samples(rows["baseline"], rows["candidate"])
        result = {
            "evaluation_run_id": judge.manifest["run_id"],
            "repeats": repeats,
            "dataset_sha256": fingerprint(dataset),
            "comparison": comparison,
            "scope": "Archived outputs rejudged; not a fresh end-to-end research comparison.",
        }
        (directory / "comparison.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        lines = [
            "# 原生 Judge 配对评估",
            "",
            result["scope"],
            "",
            "两次重复只用于观察波动，不足以证明统计显著性。均值仅统计双方均可评分的配对。",
            "",
            "| 指标 | 总配对数 | 可评分对数 | 基线均值 | 候选均值 | 平均差值 | 差值样本标准差 |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for key, value in comparison.items():
            lines.append(
                f"| {key} | {value['pairs']} | {value['scored_pairs']} | {value['baseline_mean']} | {value['candidate_mean']} | {value['mean_delta']} | {value['sample_stdev_delta']} |"
            )
        (directory / "comparison.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
    return result
