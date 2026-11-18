"""真实容器验收暴露的来源上下文和跨 UTC 日期边界回归。"""

from datetime import UTC, datetime

from agentscope.message import UserMsg
import pytest

from open_deep_research import research_time
from open_deep_research.agentscope_runtime.research_pipeline import ResearchSnapshot
from open_deep_research.agentscope_runtime.research_stages import NativeResearchStages
from open_deep_research.state import ResearchQuestion


def test_business_date_crosses_utc_midnight_boundary_and_is_frozen(monkeypatch):
    class Clock:
        @classmethod
        def now(cls, tz):
            return datetime(2026, 11, 17, 16, 15, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(research_time, "datetime", Clock)
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    state = ResearchSnapshot(run_id="date-test", config_fingerprint="fixture")
    assert state.report_date == "2026-11-18"
    monkeypatch.setenv("TZ", "UTC")
    assert research_time.research_today() == "2026-11-17"
    assert ResearchSnapshot.model_validate_json(state.model_dump_json()).report_date == "2026-11-18"


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [False, True])
async def test_brief_receives_explicit_source_scope_and_frozen_date(approved):
    class Models:
        prompt = ""

        async def structured(self, role, prompt, schema, state):
            self.prompt = prompt
            return ResearchQuestion(research_brief="比较 Dify 与 FastGPT 的部署方式。")

    selection = {"mode": "specific", "sources": [{"type": "domain", "domain": "dify.ai"}]}
    config = {"configurable": {"enable_memory": False}, "metadata": {"source_selection": selection}}
    state = ResearchSnapshot(run_id="scope-test", config_fingerprint="fixture", report_date="2026-11-18",
        messages=[UserMsg("user", "比较 Dify 与 FastGPT 的部署方式。")])
    if approved:
        state.application["approved_source_selection"] = {
            "mode": "specific", "sources": [{"type": "domain", "domain": "fastgpt.io"}]}
    models = Models()
    await NativeResearchStages(models, None, lambda: config).write_research_brief(state)
    selected = "fastgpt.io" if approved else "dify.ai"
    assert selected in models.prompt
    assert "2026-11-18" in models.prompt
    assert "用户已通过界面明确选择" in models.prompt
    assert state.coverage_contract["source_selection"]["sources"][0]["domain"] == selected
