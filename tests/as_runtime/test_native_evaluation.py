"""Native Judge rubric compatibility, durable replay and strict paired statistics."""

from types import SimpleNamespace
import json
from contextlib import asynccontextmanager

import pytest
from agentscope.model import ChatUsage, StructuredResponse

from open_deep_research.agentscope_runtime.recovery import RecoverySession
from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
from open_deep_research.agentscope_runtime.research_models import ResearchModels
from open_deep_research.evaluation.native import METRICS, evaluate_output
from open_deep_research.evaluation.paired import compare_samples
from open_deep_research.evaluation.session import NativeJudgeSession


class Factory:
    run = SimpleNamespace(get=lambda name: {})

    def __init__(self):
        self.calls = []

    def descriptor(self, role):
        return {"model": "fixture", "max_output_tokens": 1024}

    def policy_middleware(self, role, candidates=None):
        async def invoke(handler, kwargs, state):
            return await handler(self, **kwargs)

        return SimpleNamespace(policy=SimpleNamespace(invoke=invoke))

    async def generate_structured_output(self, messages, schema):
        self.calls.append(schema.__name__)
        assert messages[0].role == "system"
        assert "untrusted" in messages[0].get_text_content().lower()
        values = {"reasoning": "fixture", "score": 4}
        if schema.__name__ == "OverallQualityScore":
            values = dict.fromkeys(
                (
                    "research_depth",
                    "source_quality",
                    "analytical_rigor",
                    "practical_value",
                    "balance_and_objectivity",
                    "writing_quality",
                ),
                4,
            )
        elif schema.__name__ == "GroundednessScore":
            values = {"claims": [{"claim": "Claim A", "grounded": True}]}
        elif schema.__name__ == "EvidenceIntegrityScore":
            values = {
                "claims": [
                    {
                        "claim": "Claim A",
                        "citation": "https://example.com/a",
                        "has_citation": True,
                        "entailed_by_evidence": True,
                        "cited_source_entails_claim": True,
                        "source_authority": "primary",
                        "reasoning": "fixture",
                    }
                ],
                "reasoning": "fixture",
            }
        elif schema.__name__ == "CitationAccuracyScore":
            values = {
                "citations": [
                    {
                        "claim": "Claim A",
                        "citation": "https://example.com/a",
                        "supported": True,
                    }
                ],
                "reasoning": "fixture",
            }
        elif schema.__name__ == "ToolEfficiencyScore":
            values = {
                "tool_selection_score": 5,
                "call_efficiency_score": 4,
                "reasoning": "fixture",
            }
        return StructuredResponse(
            content=schema.model_validate(values).model_dump(),
            usage=ChatUsage(input_tokens=10, output_tokens=10, time=0.01),
        )


def output():
    return {
        "final_report": "Claim A [source](https://example.com/a)",
        "research_brief": "Research claim A",
        "evidence_registry": [
            {
                "evidence_id": "EV-A",
                "claim": "Claim A",
                "supporting_excerpt": "Claim A",
                "source_url": "https://example.com/a",
                "security_status": "accepted",
            }
        ],
    }


@pytest.mark.asyncio
async def test_service_key_judge_enforces_sql_cost_limit(tmp_path):
    from open_deep_research.configuration import Configuration
    from open_deep_research.evaluation.session import create_judge_run
    from open_deep_research.budgets import BudgetExhausted

    values = Configuration(
        model_backend="litellm",
        max_run_cost_micro_usd=37,
        max_run_model_calls=2,
        run_deadline_seconds=60,
    ).model_dump()
    run = SimpleNamespace(
        get=values.get,
        compatibility_projection=lambda: {
            "metadata": {"run_config_fingerprint": "fixture"}
        },
    )
    store = RecoveryStore("sqlite+aiosqlite:///" + (tmp_path / "cost.db").as_posix())
    await store.create_tables()
    try:
        await create_judge_run(store, "evaluation", "cost", run)
        budget = await store.budget("cost", "evaluation")
        assert budget["limits"]["cost_micro_usd"] == 37
        session = await RecoverySession.open(store, "cost", "evaluation")
        try:
            with pytest.raises(BudgetExhausted):
                await store.begin_operation(
                    session.lease,
                    "too-expensive",
                    "model_attempt",
                    {},
                    reserve={"cost_micro_usd": 38},
                )
            assert (await store.budget("cost", "evaluation"))["reserved"] == {}
        finally:
            await session.close()
    finally:
        await store.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_after_receipt", [False, True])
async def test_ten_rubrics_and_journal_replay(tmp_path, crash_after_receipt):
    from open_deep_research.agentscope_runtime.research_pipeline import ResearchSnapshot

    store = RecoveryStore("sqlite+aiosqlite:///" + (tmp_path / "judge.db").as_posix())
    await store.create_tables()
    await store.create_run(
        "evaluation", ResearchSnapshot(run_id="judge", config_fingerprint="fixed")
    )
    factory = Factory()

    class ProcessExit(BaseException):
        pass

    async def crash(point):
        if point == "operation_committed":
            raise ProcessExit()

    args = dict(
        case_id="case",
        sample_id="baseline:0",
        inputs={"messages": [{"role": "user", "content": "Claim A?"}]},
        outputs=output(),
        reference_outputs={"answer": "Claim A"},
    )
    try:
        first = await RecoverySession.open(
            store,
            "judge",
            "evaluation",
            failpoint=crash if crash_after_receipt else None,
        )
        try:
            session = NativeJudgeSession(
                ResearchModels(factory, recovery=first),
                first,
                {"evaluation_date": "2026-09-20"},
            )
            if crash_after_receipt:
                with pytest.raises(ProcessExit):
                    await session.score(**args)
            else:
                metrics = await session.score(**args)
                assert {m["evaluator"] for m in metrics} == set(METRICS)
                assert not any(m["status"] == "evaluator_error" for m in metrics)
            calls = len(factory.calls)
            assert calls > 0
        finally:
            await first.close()
        resumed = await RecoverySession.open(store, "judge", "evaluation")
        try:
            session = NativeJudgeSession(
                ResearchModels(factory, recovery=resumed),
                resumed,
                {"evaluation_date": "2026-09-20"},
            )
            replayed = await session.score(**args)
            if not crash_after_receipt:
                assert replayed == metrics
                assert len(factory.calls) == calls
            assert factory.calls.count("OverallQualityScore") == 1
            assert {m["evaluator"] for m in replayed} == set(METRICS)
            budget = await store.budget("judge", "evaluation")
            assert budget["used"]["model_calls"] == len(factory.calls)
            with pytest.raises(ValueError, match="input_changed"):
                await session.score(
                    **{**args, "outputs": {"final_report": "different"}}
                )
        finally:
            await resumed.close()
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_budget_failure_is_not_an_evaluator_error():
    from open_deep_research.budgets import BudgetDimension, BudgetExhausted

    factory = Factory()

    async def fail(*args):
        raise BudgetExhausted(BudgetDimension.MODEL_CALLS)

    factory.generate_structured_output = fail
    with pytest.raises(BudgetExhausted):
        await evaluate_output(
            ResearchModels(factory),
            case_id="a",
            sample_id="0",
            inputs={"messages": []},
            outputs=output(),
        )
    assert not factory.calls


def row(repeat, score, status="scored"):
    return {
        "case_id": "a",
        "repeat": repeat,
        "dataset_sha256": "dataset",
        "judge_sha256": "judge",
        "rubric_sha256": "rubric",
        "metric": {
            "evaluator": "relevance",
            "key": "relevance_score",
            "score": score,
            "status": status,
        },
    }


def test_pair_statistics_keep_unscored_and_reject_mismatch():
    a = [row(0, 1), row(1, 3), row(2, None, "evaluator_error")]
    b = [row(0, 2), row(1, 2), row(2, 5)]
    value = compare_samples(a, b)["relevance/relevance_score"]
    assert value["pairs"] == 3 and value["scored_pairs"] == 2
    assert value["mean_delta"] == 0
    assert value["sample_stdev_delta"] == pytest.approx(2**0.5)
    with pytest.raises(ValueError, match="set_mismatch"):
        compare_samples(a, b[:-1])
    with pytest.raises(ValueError, match="duplicate"):
        compare_samples(a, b + b[:1])
    b[0]["judge_sha256"] = "different"
    with pytest.raises(ValueError, match="fingerprint_mismatch"):
        compare_samples(a, b)


@pytest.mark.asyncio
async def test_pair_cli_artifact_freeze_and_order(tmp_path, monkeypatch):
    import hashlib
    from open_deep_research.evaluation import experiment

    case = {"id": "a", "question": "Question?", "kind": "research"}
    for side in ("baseline", "candidate"):
        content = json.dumps(
            {"question": case["question"], "status": "success", "final_report": side}
        ).encode()
        (tmp_path / (side + ".json")).write_bytes(content)
        case[side] = side + ".json"
        case[side + "_sha256"] = hashlib.sha256(content).hexdigest()
    path = tmp_path / "pairs.json"
    path.write_text(json.dumps({"schema_version": 1, "cases": [case]}), encoding="utf8")
    calls = []
    closed = []

    @asynccontextmanager
    async def judge(directory):
        async def score(**kwargs):
            calls.append(kwargs["sample_id"])
            return [row(0, 4)["metric"]]

        try:
            yield SimpleNamespace(
                score=score,
                manifest={
                    "run_id": "evaluation",
                    "judge_sha256": "fixed",
                    "evaluation_date": "2026-09-20",
                    "rubric_sha256": "rubric",
                },
            )
        finally:
            closed.append(True)

    monkeypatch.setattr(experiment, "native_judge_session", judge)
    result = await experiment.evaluate_pairs(path, tmp_path / "out")
    assert calls == ["baseline:0", "candidate:0", "candidate:1", "baseline:1"]
    assert result["comparison"]["relevance/relevance_score"]["scored_pairs"] == 2
    assert closed == [True]
    assert (tmp_path / "out/comparison.md").exists()
    with pytest.raises(ValueError, match="frozen_dataset_changed"):
        await experiment.evaluate_pairs(path, tmp_path / "out", repeats=3)
    (tmp_path / "candidate.json").write_text("{}")
    with pytest.raises(ValueError, match="hash_mismatch"):
        experiment.load_pairs(path)
    case.update(kind="knowledge", corpus_refs="TBD")
    path.write_text(json.dumps({"schema_version": 1, "cases": [case]}), encoding="utf8")
    with pytest.raises(ValueError, match="corpus_versions_required"):
        experiment.load_pairs(path)
