"""Local evaluation command line: python -m open_deep_research.evaluation."""

import argparse
import asyncio
import json
from pathlib import Path

from .artifacts import load_trials, write_json
from .statistics import compare, exit_code


def parser():
    value = argparse.ArgumentParser(
        description="本地 Agent Evals：执行、重评分、对比与人工校准"
    )
    value.add_argument(
        "--env-file", type=Path, help="仅在本进程读取的本地配置文件，不复制凭据到产物"
    )
    commands = value.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="使用原生运行时执行独立试验")
    run.add_argument("dataset", type=Path)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--mode", choices=["fixed", "live"], default="fixed")
    run.add_argument("--trials", type=int, default=1)
    run.add_argument("--judge-repeats", type=int, default=1)
    run.add_argument("--budget-usd", type=float, default=20)
    run.add_argument(
        "--case", dest="case_ids", action="append", help="只运行指定任务，可重复指定"
    )
    run.add_argument("--model", help="将研究角色固定为已授权的模型组；不改变 Judge")
    validate = commands.add_parser(
        "validate", help="免费验证评分器与正负参考产物，不执行 Agent"
    )
    validate.add_argument("dataset", type=Path)
    validate.add_argument("--output", type=Path, required=True)
    rescore = commands.add_parser(
        "rescore", help="将旧产物重评分为新产物，不重新执行 Agent"
    )
    rescore.add_argument("source", type=Path)
    rescore.add_argument(
        "--dataset",
        type=Path,
        help="可更新评分规则或参考答案，但不能改变原始问题和执行环境",
    )
    rescore.add_argument("--output", type=Path, required=True)
    rescore.add_argument("--budget-usd", type=float, default=20)
    rescore.add_argument("--judge-repeats", type=int, default=1)
    comparison = commands.add_parser("compare", help="比较同条件的基线与候选试验")
    comparison.add_argument("baseline", type=Path)
    comparison.add_argument("candidate", type=Path)
    comparison.add_argument("--output", type=Path, required=True)
    export = commands.add_parser("review-export", help="导出盲化的人工审阅文件")
    export.add_argument("source", type=Path)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--limit", type=int, default=20)
    export.add_argument("--seed", type=int, default=0)
    human = commands.add_parser("review-import", help="导入真实人工标注与争议裁决")
    human.add_argument("review", type=Path)
    human.add_argument("annotations", type=Path)
    human.add_argument("--output", type=Path, required=True)
    check = commands.add_parser("check", help="检查门禁：0通过，1失败，2无法完成判定")
    check.add_argument("source", type=Path)
    return value


def main(argv=None):
    args = parser().parse_args(argv)
    if args.env_file:
        from dotenv import load_dotenv

        load_dotenv(args.env_file)
    try:
        if args.command == "run":
            from .runner import run_dataset

            rows = asyncio.run(
                run_dataset(
                    args.dataset,
                    args.output,
                    mode=args.mode,
                    trials=args.trials,
                    judge_repeats=args.judge_repeats,
                    budget_usd=args.budget_usd,
                    case_ids=args.case_ids,
                    model=args.model,
                )
            )
            manifest = json.loads(
                (args.output / "manifest.json").read_text(encoding="utf-8")
            )
            return exit_code(
                rows, expected=len(manifest["case_ids"]) * manifest["trials"]
            )
        if args.command == "validate":
            from .runner import validate_dataset

            return exit_code(validate_dataset(args.dataset, args.output))
        if args.command == "rescore":
            from .runner import rescore

            return exit_code(
                asyncio.run(
                    rescore(
                        args.source,
                        args.output,
                        budget_usd=args.budget_usd,
                        judge_repeats=args.judge_repeats,
                        dataset_path=args.dataset,
                    )
                ),
                expected=len(load_trials(args.source)),
            )
        if args.command == "compare":
            outcome = compare(load_trials(args.baseline), load_trials(args.candidate))
            write_json(args.output, outcome)
        elif args.command == "review-export":
            from .review import export_review

            outcome = export_review(
                args.source, args.output, limit=args.limit, seed=args.seed
            )
        elif args.command == "review-import":
            from .review import import_review

            outcome = import_review(args.review, args.annotations, args.output)
        else:
            summary = json.loads(
                (args.source / "summary.json").read_text(encoding="utf-8")
            )
            return exit_code(
                load_trials(args.source), expected=summary["expected_trials"]
            )
        print(json.dumps(outcome, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:  # noqa: BLE001 -- CLI returns a sanitized error and exit code 2.
        # Error text may contain model/transport payloads: print the type only.
        print(
            json.dumps(
                {"status": "inconclusive", "error_type": type(error).__name__},
                ensure_ascii=False,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
