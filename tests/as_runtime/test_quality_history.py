"""Opt-in integration replay of a real malformed Judge receipt, without egress."""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import httpx
from agentscope.message import UserMsg
from pydantic import SecretStr
from test_recovery import create, store
from test_research_migration import cfg, contract, evidence

from open_deep_research.agentscope_runtime.gateway import SandboxBinding, SandboxChatModel
from open_deep_research.agentscope_runtime.recovery import RecoverySession
from open_deep_research.agentscope_runtime.research_agents import ResearchAssignment
from open_deep_research.agentscope_runtime.research_quality import NativeResearchQuality

pytestmark = pytest.mark.asyncio


async def test_historical_quality_pass_and_execution_limit_have_distinct_reason():
    path = os.environ.get("QUALITY_HISTORY_TASK")
    if not path:
        pytest.skip("set QUALITY_HISTORY_TASK to a historical task admission projection")
    from open_deep_research.tasks.team_service import task_view
    task = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    assert task["handoff_assessment"]["accepted"] is True
    assert task["admission_status"] == "rejected"
    assert task_view(task, summary=True)["admission_reason"] == "research_execution_not_completed: exceed_max_iters"


async def test_historical_404_diagnostic_is_not_offered_as_a_citation():
    path = os.environ.get("QUALITY_HISTORY_TOOL_RECEIPTS")
    if not path:
        pytest.skip("set QUALITY_HISTORY_TOOL_RECEIPTS to historical tool/model receipts")
    from open_deep_research.agentscope_runtime.research_agents import _compression_context
    records = [json.loads(line) for line in Path(path).read_text(encoding="utf-8-sig").splitlines()]
    urls = set()
    notes = []
    for record in records:
        result = record["result"]
        if ":tool:" in record["key"] and result["message"].get("name") == "fetch_url":
            output = json.loads(result["output"]) if isinstance(result["output"], str) else result["output"]
            urls.update(item["source_url"].rstrip("/") for item in output.get("evidence", []))
        if "model:compression" in record["key"]:
            notes.extend(block.get("text", "") for block in result["response"]["content"])
    assert "https://rocketmq.apache.org/docs/retry/" in "\n".join(notes)
    projected = _compression_context(notes, urls)
    assert "https://rocketmq.apache.org/docs/retry/" not in projected
    assert "404" in projected and all(url in projected for url in urls)


@pytest.mark.parametrize("fail_open", [True, False])
async def test_real_invalid_judge_receipt_has_durable_policy_result(store, fail_open):
    path = os.environ.get("QUALITY_HISTORY_RECEIPT")
    if not path:
        pytest.skip("set QUALITY_HISTORY_RECEIPT to an exported historical Judge response")
    body = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    assert "relevance" not in body and "corroboration" in body
    state, lease = await create(store)
    recovery = RecoverySession(store, lease, state, model_accounting="gateway")
    client = httpx.AsyncClient(trust_env=False)
    model = SandboxChatModel(binding=SandboxBinding("http://unused.invalid", state.run_id, "pipeline",
        "quality_evaluation", "researching", SecretStr("fixture")), model="if-quality-v1", structured_attempts=1, client=client)
    model._request = AsyncMock(return_value=SimpleNamespace(structured=body, finish_reason="stop"))

    class ReplayModels:
        async def structured(self, role, prompt, schema, _):
            messages = [UserMsg("user", prompt)]
            response = await recovery.model(role, messages,
                lambda: model.generate_structured_output(messages, schema), schema=schema)
            return schema.model_validate(response.content)

    config = lambda: cfg(quality_evaluation_enabled=True, quality_evaluation_min_sources=1,
                         quality_evaluation_fail_open=fail_open)
    quality = NativeResearchQuality(ReplayModels(), config)
    args = (ResearchAssignment(research_topic="市场"), contract(), [{"name": "fetch_url", "content": "fetched"}], [evidence()])
    try:
        result = await quality.batch(*args)
        assert result["evaluator_error"] and not result["accepted"]
        assert result["decision"] == ("continue" if fail_open else "complete")
        assert recovery.problem is None
        await recovery.close()
        recovery = await RecoverySession.open(store, state.run_id, "owner")
        recovery.model_accounting = "gateway"
        replayed = await quality.batch(*args)
        assert replayed["decision"] == result["decision"] and recovery.problem is None
        model._request.assert_awaited_once()
    finally:
        await recovery.close()
        await model.aclose()
        await client.aclose()
