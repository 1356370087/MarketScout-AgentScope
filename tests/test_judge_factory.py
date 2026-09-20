"""Native evaluation Judge configuration, scope and resource ownership."""

from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from open_deep_research.evaluation import judge
from open_deep_research.evaluation import session as judge_sessions
from open_deep_research.agentscope_runtime import storage


class Score(BaseModel):
    score: float


def test_judge_uses_versioned_alias_and_service_key(monkeypatch):
    monkeypatch.setenv("EVALUATION_MODEL", "if-evaluation-v2")
    monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm-proxy:4000/v1")
    monkeypatch.setenv("LITELLM_SERVICE_KEY", "service-key")
    resolved = judge.JudgeConfig.from_env()
    assert resolved.provider == "litellm"
    assert resolved.model == "if-evaluation-v2"
    assert resolved.max_retries == 0
    assert resolved.api_key == "service-key"
    assert "service-key" not in repr(resolved)


@pytest.mark.asyncio
async def test_standalone_judge_owns_native_session_and_operation_scope(monkeypatch, tmp_path):
    operations, closed = [], []
    config = judge.JudgeConfig(model="if-evaluation-v1", api_key="service-key", base_url="http://gateway/v1")

    @contextmanager
    def task(value):
        operations.append(value)
        yield

    async def structured(role, prompt, schema, state, *, messages):
        assert role == "quality_evaluation" and schema is Score
        assert operations == ["evaluation:answer"]
        assert [message.role for message in messages] == ["system", "user"]
        assert messages[-1].get_text_content() == "Question"
        return Score(score=0.7)

    @asynccontextmanager
    async def session(directory, *, judge):
        assert directory.parent == tmp_path / "evaluations"
        assert judge is config
        try:
            yield SimpleNamespace(models=SimpleNamespace(structured=structured), recovery=SimpleNamespace(task=task))
        finally:
            closed.append(True)

    monkeypatch.setattr(storage, "runtime_data_dir", lambda: tmp_path)
    monkeypatch.setattr(judge_sessions, "native_judge_session", session)
    result = await judge.invoke_judge_structured(Score, [
        {"role": "system", "content": judge.JUDGE_SECURITY_PROTOCOL},
        {"role": "user", "content": "Question"},
    ], operation="answer", config=config)
    assert result.score == 0.7 and closed == [True]


def test_judge_freezes_model_window_output_and_independent_budget():
    config = judge.JudgeConfig(model="if-evaluation-v1", api_key="service-key", base_url="http://gateway/v1")
    from open_deep_research.models.catalog import ModelCatalogEntry
    catalog = {config.model: ModelCatalogEntry(model_name=config.model, context_window=32768, max_output_tokens=3072,
                                               input_cost_per_token=0.000001, output_cost_per_token=0.000002).model_dump(mode="json")}
    run = judge_sessions.judge_run_config(config, catalog)
    assert run.get("quality_evaluation_model_max_tokens") == 3072
    assert run.get("model_catalog_snapshot") == catalog
    assert 0 < run.get("max_run_model_calls") <= 60
    assert 0 < run.get("max_run_cost_micro_usd") <= 1_000_000
    assert "service-key" not in str(run.snapshot())


@pytest.mark.asyncio
@pytest.mark.parametrize("key,retries,reason", [(None, 0, "service_key"), ("key", 1, "retries must be zero")])
async def test_judge_rejects_unconfigured_key_or_sdk_retries(monkeypatch, tmp_path, key, retries, reason):
    monkeypatch.setattr(storage, "runtime_data_dir", lambda: tmp_path)
    with pytest.raises(ValueError, match=reason):
        await judge.invoke_judge_structured(Score, [{"role": "user", "content": "Question"}], operation="answer",
            config=judge.JudgeConfig(model="if-evaluation-v1", api_key=key, base_url="http://gateway/v1", max_retries=retries))
