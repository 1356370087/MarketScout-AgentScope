"""Sequential, budgeted native experiments with immutable conditions and artifacts."""

import hashlib
import json
import os
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .artifacts import load_dataset, load_trials, report, write_json
from .budget import ExperimentBudget
from .contracts import EvalTrial
from .fixtures import FixtureEnvironment
from .graders import grade_codes
from .judge import JudgeConfig
from .local_runtime import run_native_question
from .model_grading import apply_model_samples, grade_models
from .paired import fingerprint
from .session import native_judge_session, rubric_fingerprint


@contextmanager
def environment(values):
    """Freeze case controls over ambient dotenv defaults for this sequential runner."""
    overrides = {
        key.upper(): json.dumps(value) if not isinstance(value, str) else value
        for key, value in values.items()
    }
    previous = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def configuration(case, mode, *, model=None):
    from open_deep_research.agentscope_runtime.models import ROLES
    from open_deep_research.configuration import Configuration
    from open_deep_research.security.inputs import (
        LEGACY_SANDBOX_HTTP_CONFIG_KEYS,
        PROTECTED_HTTP_CONFIG_KEYS,
    )

    cfg = Configuration.from_runnable_config(None)
    values = {
        field: getattr(cfg, field) or getattr(cfg, fallback)
        for field, fallback, _ in ROLES.values()
    }
    values.update({tokens: getattr(cfg, tokens) for _, _, tokens in ROLES.values()})
    values.update(
        enable_memory=False,
        enable_async_research=cfg.enable_async_research,
        allow_clarification=False,
        enable_human_in_loop=False,
        max_concurrent_research_units=3,
        max_researcher_iterations=4,
        max_react_tool_calls=8,
        web_pipeline_mode="enforced",
        search_api="tavily",
        model_fallbacks={},
        max_run_model_calls=80,
        run_deadline_seconds=1800,
    )
    for key in case.configuration:
        if (
            key in PROTECTED_HTTP_CONFIG_KEYS | LEGACY_SANDBOX_HTTP_CONFIG_KEYS
            or key not in Configuration.model_fields
            or any(
                word in key.lower()
                for word in ("key", "secret", "credential", "token", "base_url")
            )
            and not key.endswith("max_tokens")
        ):
            raise ValueError("unsupported_case_configuration:" + key)
    values.update(case.configuration)
    if model:
        values.update({field: model for field, _, _ in ROLES.values()})
    if mode == "fixed":
        values.update(
            sandbox_enabled=False,
            enable_memory=False,
            enable_async_research=False,
            model_backend="litellm",
        )
    elif os.getenv("EVALUATION_LOCAL_WORKERS", "false").lower() == "true":
        values.update(enable_async_research=True, async_research_mode="collaborator")
    return values


async def frozen_catalog(values, *, production=False):
    from open_deep_research.agentscope_runtime.models import ROLES
    from open_deep_research.models.catalog import (
        LiteLLMModelCatalogClient,
        freeze_catalog_snapshot,
        validate_model_catalog,
    )

    judge = JudgeConfig.from_env()
    if not judge.api_key or not judge.base_url:
        raise ValueError("missing_evaluation_service_credentials")
    client = LiteLLMModelCatalogClient(
        base_url=judge.base_url,
        api_key=os.getenv("LITELLM_MASTER_KEY") or judge.api_key,
    )
    try:
        catalog = await client.load()
    finally:
        await client.aclose()
    names = sorted(
        {v[field] for v in values for field, _, _ in ROLES.values()} | {judge.model}
    )
    if production:
        names = sorted({*names, "if-fallback-v1"})
    validate_model_catalog(catalog, names, budget_enabled=True)
    return freeze_catalog_snapshot(catalog, names)


def research_usage(view):
    budget = view.get("tool_trace", {}).get("run_metrics", {}).get("budget", {})
    physical = [
        r
        for r in view.get("operations", [])
        if r["kind"] in {"model_attempt", "gateway:model"}
        or r["kind"].startswith("model:")
        and (r.get("reservation") or {}).get("model_calls")
    ]
    known = [r for r in physical if r["state"] == "committed"]
    costs = [r["actual"].get("cost_micro_usd") for r in known if r.get("actual")]

    def total(dimension):
        values = [(r.get("actual") or {}).get(dimension) for r in known]
        return sum(values) if values and all(v is not None for v in values) else None

    return {
        "model_calls": len(physical),
        "input_tokens": total("input_tokens"),
        "output_tokens": total("output_tokens"),
        "cost_micro_usd": sum(costs)
        if costs and all(c is not None for c in costs)
        else None,
        "completeness": "complete"
        if len(known) == len(physical) and physical
        else "partial",
        "cost_source": "reported"
        if known and all(r["cost_status"] == "reported" for r in known)
        else "estimated_or_unknown",
        "budget": budget,
    }


async def score_trial(case, trial, directory, budget, *, judge_repeats):
    grade_codes(case, trial)
    if not any(g.kind == "model" for g in case.graders):
        return
    for repeat in range(judge_repeats):
        key = f"{trial.trial_id}:judge:{repeat}"
        cap = min(250_000, budget.remaining)
        budget.reserve(key, cap)
        judge_dir = Path(directory) / "judges" / trial.trial_id / str(repeat)
        started = time.monotonic()
        try:
            async with native_judge_session(
                judge_dir, cost_limit=cap, as_of=trial.fingerprints.get("as_of")
            ) as judge:
                actual_judge = fingerprint(
                    [judge.manifest["judge_sha256"], judge.manifest["evaluation_date"]]
                )
                previous_judge = trial.fingerprints.get("scoring_judge")
                if previous_judge and previous_judge != actual_judge:
                    raise ValueError("judge_changed_between_repeats")
                trial.fingerprints["scoring_judge"] = actual_judge
                trial.fingerprints["judge"] = actual_judge
                await grade_models(case, trial, judge, repeat=repeat)
        finally:
            receipt = judge_dir / "budget.json"
            if receipt.exists():
                value = json.loads(receipt.read_text(encoding="utf-8"))
                budget.settle(key, value)
                value["elapsed_seconds"] = time.monotonic() - started
                trial.usage.setdefault("judges", []).append(value)
    apply_model_samples(trial)


def agent_fingerprint():
    root = Path(__file__).parents[1]
    import importlib.metadata
    import platform

    code = {
        str(path.relative_to(root.parent)): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for folder in (root, root.parent / "security")
        for path in sorted(folder.rglob("*.py"))
        if "evaluation" not in path.relative_to(root.parent).parts
    }
    return fingerprint(
        {
            "code": code,
            "python": platform.python_version(),
            "agentscope": importlib.metadata.version("agentscope"),
        }
    )


def effective_configuration(values):
    """Reuse the runtime's credential-free configuration contract for resume checks."""
    from open_deep_research.agentscope_runtime.run_config import RunConfig

    with environment(values):
        return fingerprint(
            RunConfig.compile({"configurable": values}).compatibility_projection()[
                "configurable"
            ]
        )


async def run_dataset(
    dataset_path,
    directory,
    *,
    mode="fixed",
    trials=1,
    judge_repeats=1,
    budget_usd=20,
    case_ids=None,
    model=None,
):
    if trials < 1 or judge_repeats < 1 or budget_usd <= 0:
        raise ValueError("positive_trials_repeats_and_budget_required")
    dataset = load_dataset(dataset_path)
    cases = [c for c in dataset.cases if case_ids is None or c.id in case_ids]
    if not cases or case_ids is not None and set(case_ids) - {c.id for c in cases}:
        raise ValueError("unknown_or_empty_case_selection")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    # Exclusive process claim protects the financial journal; stale claims need review.
    lock = directory / "RUNNING"
    with lock.open("x", encoding="utf-8") as stream:
        stream.write(str(os.getpid()))
    results = []
    try:
        configs = {case.id: configuration(case, mode, model=model) for case in cases}
        catalog = (
            await frozen_catalog(list(configs.values()), production=True)
            if mode == "live"
            else await frozen_catalog(list(configs.values()))
        )
        judge_config = JudgeConfig.from_env()
        conditions = {
            "version": "1.0",
            "dataset": fingerprint(dataset.model_dump(mode="json")),
            "rubric": rubric_fingerprint(),
            "agent": agent_fingerprint(),
            "configuration": configs,
            "effective_configuration_sha256": {
                key: effective_configuration(value) for key, value in configs.items()
            },
            "catalog": catalog,
            "mode": mode,
            "trials": trials,
            "judge_repeats": judge_repeats,
            "budget_usd": budget_usd,
            "case_ids": [c.id for c in cases],
            "judge": fingerprint(
                [
                    judge_config.model,
                    judge_config.max_tokens,
                    catalog[judge_config.model],
                ]
            ),
        }
        manifest = directory / "manifest.json"
        previous = (
            json.loads(manifest.read_text(encoding="utf-8"))
            if manifest.exists()
            else {}
        )
        conditions["as_of"] = (
            previous.get("as_of") or datetime.now(UTC).date().isoformat()
        )
        if (
            manifest.exists()
            and json.loads(manifest.read_text(encoding="utf-8")) != conditions
        ):
            raise ValueError("experiment_conditions_changed_use_new_directory")
        write_json(manifest, conditions)
        write_json(directory / "dataset.json", dataset.model_dump(mode="json"))
        budget = ExperimentBudget(directory, round(budget_usd * 1_000_000))
        for case in cases:
            for repeat in range(trials):
                trial_id = f"{case.id}-{repeat:03d}"
                destination = directory / "trials" / (trial_id + ".json")
                if destination.exists():
                    saved = EvalTrial.model_validate_json(
                        destination.read_text(encoding="utf-8")
                    )
                    if saved.fingerprints.get("conditions") != fingerprint(conditions):
                        raise ValueError("trial_conditions_mismatch")
                    results.append(saved)
                    continue
                trial = EvalTrial(
                    case_id=case.id,
                    category=case.category,
                    tags=case.tags,
                    trial_id=trial_id,
                    repeat=repeat,
                    mode=mode,
                    fingerprints={
                        "dataset": conditions["dataset"],
                        "rubric": conditions["rubric"],
                        "agent": conditions["agent"],
                        "conditions": fingerprint(conditions),
                        "judge": conditions["judge"],
                        "as_of": conditions["as_of"],
                        "environment": fingerprint(
                            [
                                mode,
                                [t.model_dump(mode="json") for t in case.tools],
                                case.initial_state,
                                case.corpus_refs,
                            ]
                        ),
                    },
                )
                started = time.monotonic()
                try:
                    cap = budget.reserve(
                        trial_id + ":research", min(1_000_000, budget.remaining)
                    )
                    values = {**configs[case.id], "max_run_cost_micro_usd": cap}
                    fixture = (
                        FixtureEnvironment(case, directory / "environments" / trial_id)
                        if mode == "fixed"
                        else None
                    )
                    from open_deep_research.security.inputs import (
                        PROTECTED_HTTP_CONFIG_KEYS,
                        validate_http_configurable,
                    )

                    public_config = {
                        k: v
                        for k, v in values.items()
                        if k not in PROTECTED_HTTP_CONFIG_KEYS
                    }
                    validate_http_configurable(public_config)
                    trusted = {
                        k: v
                        for k, v in values.items()
                        if k in PROTECTED_HTTP_CONFIG_KEYS
                    }
                    with environment(
                        {
                            **values,
                            **(
                                {"as_native_resources": "gateway"}
                                if mode == "live"
                                else {}
                            ),
                        }
                    ):
                        trial.run_id, trial.outputs = await run_native_question(
                            [{"role": "user", "content": case.question}],
                            {"configurable": public_config},
                            runs_dir=directory / "runtime" / trial_id,
                            timeout=values.get("run_deadline_seconds", 1800),
                            resource_provider=fixture.resources if fixture else None,
                            interactions=case.interactions if fixture else (),
                            trusted_configuration={
                                **trusted,
                                "model_catalog_snapshot": catalog,
                            },
                        )
                    trial.elapsed_seconds = time.monotonic() - started
                    trial.runtime_status = trial.outputs["runtime_status"]
                    for field in (
                        "completed_task_outputs",
                        "supervisor_messages",
                        "notes",
                        "raw_notes",
                    ):
                        trial.outputs.pop(field, None)
                    if fixture:
                        trial.observed_state = fixture.observe()
                        trial.observed_state_complete = True
                    view = trial.outputs["evaluation_snapshot"]
                    trial.usage["research"] = research_usage(view)
                    budget.settle(
                        trial_id + ":research", trial.usage["research"]["budget"]
                    )
                    write_json(
                        destination, trial.model_dump(mode="json")
                    )  # Preserve research even if grading is interrupted.
                    await score_trial(
                        case, trial, directory, budget, judge_repeats=judge_repeats
                    )
                    # Missing pages are explicit errors in a closed corpus; successful recovery
                    # is judged by the case criteria, never by an internet fallback.
                except Exception as error:  # noqa: BLE001 -- Preserve interrupted artifacts and budget reservations.
                    trial.error = type(error).__name__ + ":evaluation_interrupted"
                    if not trial.grades:
                        grade_codes(case, trial)
                    # Infrastructure failures do not establish Agent failure.
                    if trial.runtime_status == "not_started":
                        trial.verdict = "unknown"
                finally:
                    if trial.elapsed_seconds is None:
                        trial.elapsed_seconds = time.monotonic() - started
                    write_json(destination, trial.model_dump(mode="json"))
                results.append(trial)
                report(directory, results, expected=len(cases) * trials)
                if trial.error:
                    return results
        report(directory, results, expected=len(cases) * trials)
        return results
    finally:
        lock.unlink()


def validate_dataset(dataset_path, directory):
    """Free grader validation against authored controls; never runs an Agent."""
    dataset = load_dataset(dataset_path)
    directory = Path(directory)
    results = []
    write_json(directory / "dataset.json", dataset.model_dump(mode="json"))
    for case in dataset.cases:
        code_case = case.model_copy(
            update={"graders": [g for g in case.graders if g.kind == "code"]}
        )
        controls = [("positive", case.reference_trial, "pass")]
        controls.extend(
            (f"negative-{i}", row, row.get("expected_verdict", "fail"))
            for i, row in enumerate(case.negative_trials)
        )
        for index, (name, raw, expected) in enumerate(controls):
            data = {k: v for k, v in (raw or {}).items() if k != "expected_verdict"}
            trial = EvalTrial(
                case_id=case.id,
                trial_id=f"{case.id}-{name}",
                repeat=index,
                mode="reference",
                **data,
            )
            grade_codes(code_case, trial)
            actual = trial.verdict
            trial.verdict = "pass" if raw is not None and actual == expected else "fail"
            trial.error = (
                None
                if trial.verdict == "pass"
                else f"grader_control_mismatch:expected={expected},actual={actual}"
            )
            write_json(
                directory / "trials" / (trial.trial_id + ".json"),
                trial.model_dump(mode="json"),
            )
            results.append(trial)
    report(directory, results, expected=len(results))
    return results


async def rescore(
    source, destination, *, budget_usd=20, judge_repeats=1, dataset_path=None
):
    if judge_repeats < 1 or budget_usd <= 0:
        raise ValueError("positive_repeats_and_budget_required")
    source, destination = Path(source), Path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("rescore_destination_must_be_empty")
    original = load_dataset(source / "dataset.json")
    dataset = load_dataset(dataset_path) if dataset_path else original
    original_cases = {c.id: c for c in original.cases}
    for case in dataset.cases:
        old = original_cases.get(case.id)
        if old is None or any(
            getattr(old, field) != getattr(case, field)
            for field in (
                "question",
                "tools",
                "configuration",
                "initial_state",
                "interactions",
                "corpus_refs",
            )
        ):
            raise ValueError("rescore_cannot_change_agent_input_or_environment")
    cases = {c.id: c for c in dataset.cases}
    rows = load_trials(source)
    if any(trial.case_id not in cases for trial in rows):
        raise ValueError("rescore_dataset_missing_source_case")
    budget = ExperimentBudget(destination, round(budget_usd * 1_000_000))
    write_json(destination / "dataset.json", dataset.model_dump(mode="json"))
    results = []
    for trial in rows:
        trial.fingerprints["source_artifact"] = fingerprint(
            trial.model_dump(mode="json")
        )
        trial.fingerprints["rubric"] = rubric_fingerprint()
        trial.fingerprints["dataset"] = fingerprint(dataset.model_dump(mode="json"))
        trial.fingerprints["as_of"] = datetime.now(UTC).date().isoformat()
        trial.fingerprints.pop("scoring_judge", None)
        trial.error = None
        trial.judge_samples = []
        trial.usage["source_judges"] = trial.usage.pop("judges", [])
        try:
            await score_trial(
                cases[trial.case_id],
                trial,
                destination,
                budget,
                judge_repeats=judge_repeats,
            )
        except Exception as error:  # noqa: BLE001 -- Preserve failed grading and unresolved cost without retrying.
            trial.error = type(error).__name__ + ":evaluation_interrupted"
        write_json(
            destination / "trials" / (trial.trial_id + ".json"),
            trial.model_dump(mode="json"),
        )
        results.append(trial)
        report(destination, results, expected=len(rows))
        if trial.error:
            return results
    report(destination, results, expected=len(rows))
    return results
