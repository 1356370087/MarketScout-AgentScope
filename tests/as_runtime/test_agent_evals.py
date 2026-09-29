"""Behavioral regression tests for trusted grades, native traces and experiments."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from open_deep_research.evaluation.artifacts import (
    load_dataset,
    load_trials,
    write_json,
)
from open_deep_research.evaluation.budget import ExperimentBudget
from open_deep_research.evaluation.contracts import (
    EvalCase,
    EvalTrial,
    GraderResult,
    GraderSpec,
)
from open_deep_research.evaluation.graders import grade_codes
from open_deep_research.evaluation.legacy_cli import aggregate_score, assess_quality
from open_deep_research.evaluation.metrics import primary_metrics
from open_deep_research.evaluation.native import complete_metric_categories
from open_deep_research.evaluation.statistics import compare, exit_code, summarize

DATASET = Path(__file__).parents[1] / "fixtures/agent_evals/core.json"


def trial(case="a", repeat=0, verdict="pass", **kwargs):
    kwargs.setdefault("runtime_status", "completed")
    return EvalTrial(
        case_id=case,
        trial_id=f"{case}-{repeat}",
        repeat=repeat,
        mode="fixed",
        verdict=verdict,
        fingerprints=dict.fromkeys(
            ("dataset", "rubric", "environment", "judge"), "fixed"
        ),
        **kwargs,
    )


def case_for(check, params, target="outcome"):
    return EvalCase(
        id="a",
        question="Research",
        category="fixture",
        graders=[GraderSpec(id="check", check=check, parameters=params, target=target)],
    )


def strong_metrics():
    mapping = {
        "source_quality_score": "overall_quality",
        "source_authority_score": "evidence_integrity",
        "groundedness_score": "evidence_integrity",
        "citation_accuracy_score": "evidence_integrity",
        "completeness_score": "completeness",
        "judge_consistency_score": "consistency_reconciler",
    }
    return [
        {"evaluator": e, "key": k, "score": 0.9, "status": "scored"}
        for k, e in mapping.items()
    ]


def test_non_applicable_constraint_does_not_turn_quality_into_failure():
    from open_deep_research.evaluation.evaluators import eval_execution_compliance
    from open_deep_research.evaluation.metrics import normalize_evaluator_metric

    explicit = normalize_evaluator_metric(
        "execution_compliance", eval_execution_compliance({}, {})
    )
    rows = complete_metric_categories([*strong_metrics(), explicit])
    assert assess_quality(rows, aggregate=0.9)["passed"]
    execution = next(row for row in rows if row["key"] == "execution_compliance_score")
    assert execution["status"] == "not_applicable"


def test_canonical_metric_controls_both_aggregate_and_gate_in_any_order():
    rows = strong_metrics() + [
        {
            "evaluator": "groundedness",
            "key": "groundedness_score",
            "score": 0,
            "status": "scored",
        }
    ]
    for ordered in (rows, rows[::-1]):
        assert (
            len(
                [
                    r
                    for r in primary_metrics(ordered)
                    if r["key"] == "groundedness_score"
                ]
            )
            == 1
        )
        score = aggregate_score(ordered)
        assert score == pytest.approx(0.9)
        assert assess_quality(ordered, aggregate=score)["passed"]
    with pytest.raises(ValueError, match="ambiguous"):
        primary_metrics([{"key": "x", "score": 0.1}, {"key": "x", "score": 0.9}])


def test_absent_or_partial_trace_never_calls_judge(monkeypatch):
    from open_deep_research.evaluation import evaluators

    def forbidden(*args, **kwargs):
        pytest.fail("Judge must not invent a score for missing observations")

    monkeypatch.setattr(evaluators, "_invoke_structured_output", forbidden)
    for trace in (
        {},
        {"completeness": "partial", "supervisor_tool_calls": [{"name": "fetch_url"}]},
    ):
        value = evaluators.eval_tool_efficiency(
            {},
            {
                "final_report": "report",
                "evaluation_snapshot": {"schema_version": "2.0", "tool_trace": trace},
            },
        )
        assert (
            value["score"] is None
            and value["metadata"]["metric_status"] == "not_scored"
        )


def test_numeric_control_accepts_markdown_and_natural_labels():
    case = load_dataset(DATASET).cases[0]
    value = trial(outputs={"final_report": "2025 年收入为 **120 万元**。"})
    grade_codes(case, value)
    assert value.verdict == "pass"
    value.outputs["final_report"] = "2025 年收入为 **120.1 万元**。"
    grade_codes(case, value)
    assert value.verdict == "fail"


def test_tool_success_does_not_hide_failed_research_or_missing_setup():
    case = case_for(
        "tools", {"required": [{"name": "fetch_url", "succeeded": True}]}, "trace"
    )
    value = trial(
        runtime_status="failed",
        outputs={
            "evaluation_snapshot": {
                "tool_trace": {
                    "completeness": "complete",
                    "researcher_tool_calls": [
                        {"name": "fetch_url", "state": "committed"}
                    ],
                }
            }
        },
    )
    grade_codes(case, value)
    assert value.verdict == "fail"
    assert next(g for g in value.grades if g.grader_id == "check").verdict == "pass"
    value.outputs["result"] = {"error": "ModelCatalogError"}
    grade_codes(case, value)
    assert value.verdict == "unknown"


def test_missing_execution_category_is_unknown_until_applicability_is_known():
    rows = complete_metric_categories(strong_metrics())
    assert (
        next(m for m in rows if m["key"] == "execution_compliance_score")["status"]
        == "not_scored"
    )
    assert not assess_quality(rows, aggregate=0.9)["passed"]


def test_gateway_provider_rejection_is_unknown_but_does_not_hide_quality_or_safety():
    case = case_for("state", {"allowed": ["completed"]})
    value = trial(
        runtime_status="failed",
        outputs={
            "result": {"error": "GatewayCallError"},
            "evaluation_snapshot": {
                "operations": [
                    {"kind": "gateway:model", "error": "budget_or_rate_limit"},
                ]
            },
        },
    )
    grade_codes(case, value)
    assert value.verdict == "unknown"
    value.outputs["evaluation_snapshot"]["operations"].append(
        {"kind": "gateway:model", "error": None}
    )
    grade_codes(case, value)
    assert (
        value.verdict == "fail"
    )  # A recovered rejection cannot explain a later failure.
    value.outputs["evaluation_snapshot"]["operations"].pop()
    value.observed_state_complete = True
    value.observed_state = {"outbound": ["private"]}
    case.graders.append(
        GraderSpec(
            id="no-egress",
            check="safety",
            target="safety",
            parameters={"forbidden_effects": ["outbound"]},
        )
    )
    grade_codes(case, value)
    assert value.verdict == "fail"


def test_missing_evidence_and_wrong_effect_are_different_outcomes():
    case = case_for("impact", {"path": "booked", "expected": True}, "impact")
    value = trial(outputs={"final_report": "Your booking succeeded"})
    grade_codes(case, value)
    assert value.verdict == "unknown"
    value.observed_state_complete, value.observed_state = True, {"booked": False}
    grade_codes(case, value)
    assert value.verdict == "fail"
    value.observed_state["booked"] = True
    grade_codes(case, value)
    assert value.verdict == "pass"


def test_known_safety_violation_wins_over_incomplete_trace():
    value = trial(
        observed_state_complete=True, observed_state={"outbound": ["private"]}
    )
    grade_codes(
        case_for("safety", {"forbidden_effects": ["outbound"]}, "safety"), value
    )
    assert value.verdict == "fail"


def test_denied_attack_is_not_successful_execution():
    case = case_for("safety", {"forbidden_executions": ["transmit"]}, "safety")
    call = {
        "name": "transmit",
        "args": {},
        "state": "committed",
        "error": {"error_type": "permission_denied"},
    }
    value = trial(
        outputs={
            "evaluation_snapshot": {
                "tool_trace": {
                    "completeness": "complete",
                    "researcher_tool_calls": [call],
                }
            }
        }
    )
    grade_codes(case, value)
    assert value.verdict == "pass"
    call["error"] = None
    grade_codes(case, value)
    assert value.verdict == "fail"


@pytest.mark.parametrize("state", ["waiting", "cancelled", "failed"])
def test_case_can_expect_a_non_report_outcome(state):
    value = trial(runtime_status=state)
    grade_codes(case_for("state", {"allowed": [state]}), value)
    assert value.verdict == "pass"


def test_k_metrics_measure_agent_trials_and_expose_unknowns():
    rows = [trial(repeat=i, verdict="pass" if i < 2 else "fail") for i in range(3)]
    value = summarize(rows)["cases"]["a"]["reliability"]["3"]
    assert value == {"pass_at_k": 1, "pass_power_k": 0, "reason": None}
    rows[0].judge_samples = [[{}], [{}]]
    assert summarize(rows)["cases"]["a"]["trials"] == 3
    rows[2].verdict = "unknown"
    assert summarize(rows)["cases"]["a"]["reliability"]["3"]["pass_at_k"] is None


def test_task_bootstrap_rejects_unmatched_conditions_and_small_sample_claims():
    old = [trial(case="a", verdict="fail"), trial(case="b")]
    new = [trial(case="a"), trial(case="b")]
    value = compare(old, new)
    assert value["mean_success_rate_delta"] == 0.5
    assert value["improvement_supported"] is False
    new[0].fingerprints["rubric"] = "different"
    with pytest.raises(ValueError, match="fingerprint_mismatch"):
        compare(old, new)


def test_financial_reservations_survive_restart_and_unknown_receipts(tmp_path):
    budget = ExperimentBudget(tmp_path, 100)
    budget.reserve("a", 60)
    budget.settle("a", {})
    restored = ExperimentBudget(tmp_path, 100)
    assert restored.remaining == 40
    with pytest.raises(ValueError, match="exhausted"):
        restored.reserve("b", 41)
    with pytest.raises(ValueError, match="reconcile"):
        restored.reserve("a", 30)
    restored.settle(
        "a", {"used": {"cost_micro_usd": 20}, "reserved": {"cost_micro_usd": 10}}
    )
    assert restored.remaining == 70
    assert restored.data["allocations"]["a"]["status"] == "unresolved"


def test_cli_exit_does_not_hide_failure_behind_unscored_or_missing_trials():
    assert exit_code([trial()], expected=2) == 2
    assert exit_code([trial(verdict="fail"), trial(case="b", verdict="unknown")]) == 1
    assert exit_code([trial()]) == 0


def test_seed_dataset_controls_all_pass_without_model_calls(tmp_path):
    from open_deep_research.evaluation.runner import validate_dataset

    cases = load_dataset(DATASET).cases
    assert len(cases) == 24
    assert all(case.reference_trial and case.negative_trials for case in cases)
    rows = validate_dataset(DATASET, tmp_path)
    assert len(rows) == 48 and all(row.verdict == "pass" for row in rows)
    assert (
        summarize(rows)["cases"] == {}
    )  # Controls must not become agent reliability data.


@pytest.mark.asyncio
async def test_sql_trace_retains_arguments_denials_and_excludes_public_export(tmp_path):
    from open_deep_research.agentscope_runtime.recovery import RecoverySession
    from open_deep_research.agentscope_runtime.recovery_events import public_events
    from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
    from open_deep_research.agentscope_runtime.research_pipeline import ResearchSnapshot
    from open_deep_research.evaluation.trace import collect_native_snapshot
    from open_deep_research.tools.base import ToolEffect, ToolExecutionZone, ToolResult
    from open_deep_research.tools.governance import (
        GovernedToolCallResult,
        ToolOutcomeMessage,
    )

    store = RecoveryStore("sqlite+aiosqlite:///" + (tmp_path / "runs.db").as_posix())
    await store.create_tables()
    state = ResearchSnapshot(
        run_id="trace-test",
        config_fingerprint="fixed",
        application={"evaluation_capture": True},
    )
    await store.create_run("owner", state)
    session = await RecoverySession.open(store, state.run_id, "owner")
    tool = SimpleNamespace(
        name="fetch_url",
        effect=ToolEffect.READ_ONLY,
        execution_zone=ToolExecutionZone.GATEWAY,
        supports_idempotency=False,
    )
    effects = []

    async def execute():
        effects.append("read")
        return GovernedToolCallResult(
            ToolOutcomeMessage("read", "fetch_url", "c"), ToolResult(output="public")
        )

    try:
        args = {"url": "https://example.com/", "api_key": "synthetic-credential"}
        with session.task("researcher-one"):
            await session.tool(tool, "c", args, execute)
            await session.tool(tool, "c", args, execute)
        view = await collect_native_snapshot(store, state, "owner")
        assert view["tool_trace"]["completeness"] == "complete"
        calls = view["tool_trace"]["researcher_tool_calls"]
        assert len(calls) == 1 and calls[0]["args"]["url"] == args["url"]
        assert calls[0]["args"]["api_key"] == "[REDACTED]"
        assert effects == ["read"]
        assert "synthetic-credential" not in json.dumps(view)
        events = await store.events(state.run_id, "owner")
        private = [e for e in events if e["payload"]["type"].startswith("evaluation.")]
        assert private and all(public_events(e) == [] for e in private)
    finally:
        await session.close()
        await store.aclose()


def test_human_export_blinds_scores_and_import_preserves_disagreement(tmp_path):
    from open_deep_research.evaluation.review import export_review, import_review

    source, review = tmp_path / "source", tmp_path / "review"
    case = load_dataset(DATASET).cases[0]
    write_json(source / "dataset.json", {"cases": [case.model_dump(mode="json")]})
    original = trial(
        case=case.id,
        outputs={"final_report": "收入120万元"},
        grades=[
            GraderResult(
                grader_id="quality-model",
                kind="model",
                target="quality",
                required=True,
                verdict="pass",
                score=0.9,
                reason="MODEL_SECRET_REASON",
            )
        ],
    )
    write_json(
        source / "trials" / (original.trial_id + ".json"),
        original.model_dump(mode="json"),
    )
    assert export_review(source, review)["samples"] == 1
    text = (review / "sample-001.md").read_text(encoding="utf-8")
    assert "MODEL_SECRET_REASON" not in text and original.trial_id not in text
    annotations = tmp_path / "labels.jsonl"
    labels = [
        {
            "sample_id": "sample-001",
            "criterion": "quality",
            "reviewer_id": "a",
            "score": 1,
            "verdict": "pass",
            "reason": "good",
        },
        {
            "sample_id": "sample-001",
            "criterion": "quality",
            "reviewer_id": "b",
            "score": 0,
            "verdict": "fail",
            "reason": "bad",
        },
    ]
    annotations.write_text("\n".join(json.dumps(r) for r in labels), encoding="utf-8")
    outcome = import_review(review, annotations, tmp_path / "derived")
    assert outcome["disagreements"] and outcome["inter_rater_agreement"] == 0
    assert outcome["model_human_pairs"] == 0
    labels.append(
        {
            "sample_id": "sample-001",
            "criterion": "quality",
            "reviewer_id": "expert",
            "score": 1,
            "verdict": "pass",
            "reason": "adjudicated",
            "adjudication": True,
        }
    )
    annotations.write_text("\n".join(json.dumps(r) for r in labels), encoding="utf-8")
    outcome = import_review(review, annotations, tmp_path / "adjudicated")
    assert not outcome["disagreements"] and outcome["model_human_agreement"] == 1
    assert load_trials(source)[0].grades == original.grades


def test_human_labels_cannot_attach_to_changed_artifact(tmp_path):
    from open_deep_research.evaluation.review import export_review, import_review

    source, review = tmp_path / "source", tmp_path / "review"
    case = load_dataset(DATASET).cases[0]
    write_json(source / "dataset.json", {"cases": [case.model_dump(mode="json")]})
    value = trial(case=case.id)
    path = source / "trials" / (value.trial_id + ".json")
    write_json(path, value.model_dump(mode="json"))
    export_review(source, review)
    value.outputs["final_report"] = "changed"
    write_json(path, value.model_dump(mode="json"))
    labels = tmp_path / "labels.jsonl"
    labels.write_text(
        json.dumps(
            {
                "sample_id": "sample-001",
                "reviewer_id": "human",
                "criterion": "quality",
                "verdict": "pass",
                "score": 1,
                "reason": "reviewed",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="source_changed"):
        import_review(review, labels, tmp_path / "derived")


@pytest.mark.asyncio
async def test_fixed_environment_uses_receiver_state_and_never_falls_back(tmp_path):
    from open_deep_research.evaluation.fixtures import FixtureEnvironment

    case = load_dataset(DATASET).cases[0]
    env = FixtureEnvironment(case, tmp_path / "one")
    tool = env.tools()[0]
    args = tool.input_schema(url="https://unknown.test/")
    with pytest.raises(ValueError, match="fixture_response_not_found"):
        await tool.call(args, SimpleNamespace())
    assert env.observe()["fixture_misses"]
    other = FixtureEnvironment(case, tmp_path / "two")
    assert other.observe()["fixture_misses"] == []
    url = case.tools[0].responses[0]["match"]["url"]
    answer = await tool.call(tool.input_schema(url=url + "#section"), SimpleNamespace())
    assert answer.output["evidence_registry"]


@pytest.mark.asyncio
async def test_owner_probe_uses_native_sql_authority_not_model_supplied_owner(tmp_path):
    from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
    from open_deep_research.evaluation.fixtures import FixtureEnvironment

    store = RecoveryStore(
        "sqlite+aiosqlite:///" + (tmp_path / "ownership.db").as_posix()
    )
    await store.create_tables()
    case = next(c for c in load_dataset(DATASET).cases if c.id == "tenant-boundary")
    env = FixtureEnvironment(case, tmp_path / "receiver")
    try:
        await env.bind_owner_records(
            SimpleNamespace(
                store=store,
                lease=SimpleNamespace(user_id="caller", run_id="evaluation"),
            )
        )
        tool = next(t for t in env.tools() if t.name == "read_run")
        context = SimpleNamespace(config={"metadata": {"user_id": "caller"}})
        own = await tool.call(tool.input_schema(record="own"), context)
        assert own.output["text"]
        with pytest.raises(KeyError):
            await tool.call(tool.input_schema(record="foreign"), context)
        assert env.observe()["foreign_records"] == []
        with pytest.raises(ValueError):
            tool.input_schema(record="foreign", owner_id="fixture-foreign-owner")
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_callback_listener_closes_its_owned_port(unused_tcp_port):
    import httpx

    from open_deep_research.evaluation.callbacks import gateway_callbacks

    async def inactive(run_id):
        return None

    service = SimpleNamespace(pipeline_factory=SimpleNamespace(gateway_ledger=inactive))
    url = f"http://127.0.0.1:{unused_tcp_port}"
    async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
        async with gateway_callbacks(service, host="127.0.0.1", port=unused_tcp_port):
            response = await client.get(url + "/openapi.json")
            assert response.status_code == 200
            assert "/internal/sandbox/budgets/reserve" in response.json()["paths"]
        with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
            await client.get(url + "/openapi.json")
        async with gateway_callbacks(service, host="127.0.0.1", port=unused_tcp_port):
            assert (await client.get(url + "/openapi.json")).status_code == 200


@pytest.mark.asyncio
async def test_effect_verifier_reads_file_and_db_even_if_receipt_is_lost(tmp_path):
    import hashlib

    from open_deep_research.evaluation.fixtures import FixtureEnvironment

    case = EvalCase(
        id="write",
        question="write the fixture",
        category="impact",
        graders=[
            GraderSpec(
                id="file",
                check="file",
                target="impact",
                parameters={
                    "path": "report.md",
                    "expected": {"sha256": hashlib.sha256(b"verified").hexdigest()},
                },
            )
        ],
        tools=[
            {
                "name": "write_file",
                "description": "write fixture",
                "effect": "local_write",
                "responses": [
                    {
                        "match": {},
                        "effect": {
                            "file": {"path": "report.md", "text": "verified"},
                            "set": {"published": True},
                        },
                        "after_effect_error": True,
                    }
                ],
            }
        ],
    )
    env = FixtureEnvironment(case, tmp_path)
    tool = env.tools()[0]
    with pytest.raises(RuntimeError, match="unknown_effect"):
        await tool.call(tool.input_schema(), SimpleNamespace())
    value = trial(observed_state_complete=True, observed_state=env.observe())
    grade_codes(case, value)
    assert value.verdict == "pass" and value.observed_state["effect_count"] == 1
    assert value.observed_state["published"] is True


@pytest.mark.asyncio
async def test_runner_freezes_conditions_and_does_not_repeat_finished_research(
    tmp_path, monkeypatch
):
    from open_deep_research.evaluation import runner
    from open_deep_research.evaluation.artifacts import load_trials

    calls = []
    case = load_dataset(DATASET).cases[0]
    dataset = tmp_path / "dataset.json"
    write_json(
        dataset,
        {"name": "test", "version": "1", "cases": [case.model_dump(mode="json")]},
    )

    async def catalog(values):
        return {runner.JudgeConfig.from_env().model: {"fixture": True}}

    async def research(messages, config, **kwargs):
        calls.append(kwargs["runs_dir"])
        output = dict(case.reference_trial["outputs"])
        output["runtime_status"] = "completed"
        output["evaluation_snapshot"] = json.loads(
            json.dumps(output["evaluation_snapshot"])
        )
        output["evaluation_snapshot"]["tool_trace"]["run_metrics"] = {
            "budget": {"used": {"cost_micro_usd": 10}, "reserved": {}}
        }
        assert "sandbox_enabled" not in config["configurable"]
        assert kwargs["trusted_configuration"]["sandbox_enabled"] is False
        return "run-fixture", output

    monkeypatch.setattr(runner, "frozen_catalog", catalog)
    monkeypatch.setattr(runner, "run_native_question", research)
    rows = await runner.run_dataset(dataset, tmp_path / "experiment", trials=2)
    assert len(calls) == 2 and calls[0] != calls[1]
    assert all(r.verdict == "pass" for r in rows)
    await runner.run_dataset(dataset, tmp_path / "experiment", trials=2)
    assert len(calls) == 2
    with pytest.raises(ValueError, match="conditions_changed"):
        await runner.run_dataset(dataset, tmp_path / "experiment", trials=3)
    assert len(load_trials(tmp_path / "experiment")) == 2


@pytest.mark.asyncio
async def test_rescore_never_restarts_agent_and_preserves_source(tmp_path, monkeypatch):
    from open_deep_research.evaluation import runner

    case = load_dataset(DATASET).cases[0]
    source, dest = tmp_path / "source", tmp_path / "derived"
    write_json(
        source / "dataset.json",
        {"name": "test", "version": "1", "cases": [case.model_dump(mode="json")]},
    )
    old = trial(case=case.id, outputs=case.reference_trial["outputs"])
    path = source / "trials" / (old.trial_id + ".json")
    write_json(path, old.model_dump(mode="json"))
    before = path.read_bytes()

    async def forbidden(*args, **kwargs):
        pytest.fail("rescore must not execute research")

    monkeypatch.setattr(runner, "run_native_question", forbidden)
    rows = await runner.rescore(source, dest)
    assert rows[0].verdict == "pass" and path.read_bytes() == before


@pytest.mark.asyncio
async def test_rescore_preserves_interrupted_judge_and_never_retries_unknown_cost(
    tmp_path, monkeypatch
):
    from contextlib import asynccontextmanager

    from open_deep_research.evaluation import runner

    case = case_for("state", {"allowed": ["completed"]})
    case.graders.append(
        GraderSpec(id="answer", check="correctness_score", kind="model")
    )
    source, dest = tmp_path / "source", tmp_path / "derived"
    write_json(
        source / "dataset.json",
        {
            "name": "test",
            "version": "1",
            "cases": [case.model_dump(mode="json")],
        },
    )
    for repeat in range(2):
        old = trial(repeat=repeat)
        write_json(
            source / "trials" / (old.trial_id + ".json"), old.model_dump(mode="json")
        )
    before = {path: path.read_bytes() for path in source.rglob("*.json")}
    calls = []

    @asynccontextmanager
    async def session(*args, **kwargs):
        yield SimpleNamespace(
            manifest={"judge_sha256": "fixed", "evaluation_date": "2026-09-22"}
        )

    async def interrupted(*args, **kwargs):
        calls.append(True)
        raise ConnectionError("unknown paid operation")

    monkeypatch.setattr(runner, "native_judge_session", session)
    monkeypatch.setattr(runner, "grade_models", interrupted)
    rows = await runner.rescore(source, dest, budget_usd=1)
    saved = load_trials(dest)
    assert len(rows) == len(saved) == len(calls) == 1
    assert saved[0].error == "ConnectionError:evaluation_interrupted"
    assert saved[0].verdict == "unknown"
    summary = json.loads((dest / "summary.json").read_text(encoding="utf-8"))
    assert summary["expected_trials"] == 2 and summary["recorded_trials"] == 1
    assert exit_code(saved, expected=2) == 2
    allocation = next(
        iter(
            json.loads((dest / "spend.json").read_text(encoding="utf-8"))[
                "allocations"
            ].values()
        )
    )
    assert (
        allocation["status"] == "reserved"
        and allocation["charged_micro_usd"] == 250_000
    )
    with pytest.raises(ValueError, match="destination_must_be_empty"):
        await runner.rescore(source, dest, budget_usd=1)
    assert len(calls) == 1
    assert all(path.read_bytes() == data for path, data in before.items())
