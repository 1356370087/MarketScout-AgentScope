"""Sandbox report budgets use the frozen catalog, including real recovery input."""

import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from open_deep_research.agentscope_runtime.gateway import SandboxBinding
from open_deep_research.agentscope_runtime.models import CredentialBinding, ModelFactory
from open_deep_research.agentscope_runtime.report import _ReportRun
from open_deep_research.agentscope_runtime.research_models import ResearchModels
from open_deep_research.agentscope_runtime.run_config import RunConfig
from open_deep_research.configuration import Configuration
from open_deep_research.report.models import ReportDraft, ReportReview
from open_deep_research.report.reviewer import _invoke_reviewer, _invoke_reviser, _revision_prompt
from open_deep_research.report.runtime import native_report
from open_deep_research.report.writing import ReportInputBudgetExceeded
from tests.as_runtime.test_report_native import state


@pytest.mark.asyncio
@pytest.mark.parametrize("window", [131072, 24000])
@pytest.mark.parametrize("role", ["report_review", "report_revisor"])
async def test_real_recovery_review_uses_frozen_sandbox_window(window, role):
    # Domain-only payload captured by replaying the six committed report receipts
    # from Web run 09ac26e79e6750ae8c8cca9b2e53855c; no identity or credentials.
    payload = json.loads((Path(__file__).parents[1] / "fixtures" /
                          "report-recovery-review-input.json").read_text(encoding="utf8"))
    spec = "review-fixture"
    run = RunConfig.compile({"configurable": {
        "report_review_model": spec, "report_review_model_max_tokens": 3072,
        "final_report_model": spec, "final_report_model_max_tokens": 10000,
        "model_catalog_snapshot": {spec: {"model_name": spec,
            "context_window": window, "max_output_tokens": 32768,
            "input_cost_per_token": 0, "output_cost_per_token": 0}},
    }})
    # Exercise the restored factory used by production, not a synthetic context_size.
    run = RunConfig.restore(run.snapshot())
    factory = ModelFactory(run, scope="run", owner="native-report", bindings={
        role: CredentialBinding("fixture", "run", "native-report",
                                (spec,), SecretStr("fixture"))})
    requests = []

    async def serve(request):
        body = json.loads(request.content)
        requests.append(body)
        fields = json.loads(body["messages"][1]["content"][0]["text"])
        evidence = json.loads(body["messages"][2]["content"][0]["text"])
        assert fields["draft_markdown"] == payload["draft_markdown"]
        assert evidence["records"] == payload["evidence_registry"]
        assert evidence["omitted_record_count"] == 0
        if role == "report_revisor":
            assert fields["review"]["issues"][0]["description"].endswith("ISSUE-END")
        return httpx.Response(200, json={
            "protocol_version": 2, "logical_operation_id": body["logical_operation_id"],
            "requested_model": spec, "status": "completed",
            "message": {"role": "assistant", "content": payload["draft_markdown"]},
            "structured": {"decision": "revise"} if role == "report_review" else None,
            "finish_reason": "stop",
            "usage": {"input_tokens": 10000, "output_tokens": 10},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve),
                                base_url="https://gateway.invalid") as client:
        def model_for(role, task):
            return factory.build_sandbox(role, SandboxBinding(
                "https://gateway.invalid", "native-report", task, role, "writing",
                SecretStr("fixture")), client=client)

        port = _ReportRun(ResearchModels(factory, model_for=model_for), state())
        token = native_report.set(port)
        try:
            cfg = Configuration.from_runnable_config(run.compatibility_projection())
            async def invoke():
                if role == "report_review":
                    return await _invoke_reviewer(payload, {}, cfg, attempt=3)
                prompt = _revision_prompt(
                    ReportDraft(markdown=payload["draft_markdown"]),
                    ReportReview(issues=[{"description": "Revise duplicated sections. ISSUE-END"}]),
                    payload, run.compatibility_projection(),
                )
                return await _invoke_reviser(prompt, {}, cfg)

            if window == 24000:
                with pytest.raises(ReportInputBudgetExceeded, match="fixed_context"):
                    await invoke()
                assert requests == []
            else:
                result = await invoke()
                if role == "report_review":
                    assert result.decision == "revise"
                else:
                    assert result == payload["draft_markdown"]
                assert len(requests) == 1
            assert model_for(role, "report:lead.report_review:0").context_size == window
        finally:
            native_report.reset(token)
            await factory.aclose()
