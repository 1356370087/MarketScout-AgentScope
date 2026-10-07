"""Regressions discovered by real Docker Web research with quality enabled."""

import pytest

from open_deep_research.agentscope_runtime.search_providers import source_allowed
from open_deep_research.quality.contract import build_research_coverage_contract

QUESTION = (
    "仅使用 PostgreSQL 官方文档，核实 PostgreSQL 17 的增量备份支持："
    "说明如何生成增量备份、恢复前如何合并、与常规基础备份的关系和主要限制。"
    "至少引用3个不同的官方文档页面并给出可核验链接。"
    "报告使用中文，控制在1000字左右，不需要性能跑分或市场分析。"
)


def contract():
    return build_research_coverage_contract([{"role": "user", "content": QUESTION}])


def test_research_contract_separates_source_and_writing_instructions():
    kinds = {item.text: item.kind for item in contract().requirements}
    assert kinds["仅使用 PostgreSQL 官方文档"] == "process"
    assert kinds["至少引用3个不同的官方文档页面并给出可核验链接"] == "process"
    assert kinds["报告使用中文"] == "deliverable"
    assert kinds["控制在1000字左右"] == "deliverable"
    assert kinds["不需要性能跑分或市场分析"] == "process"
    assert kinds["恢复前如何合并"] == "factual"
    assert kinds["与常规基础备份的关系"] == "factual"
    assert kinds["主要限制"] == "factual"


@pytest.mark.parametrize("url,allowed", [
    ("https://www.postgresql.org/docs/17/app-pgbasebackup.html", True),
    ("https://www.postgresql.org/docs/17/app-pgcombinebackup.html", True),
    ("https://www.dbi-services.com/blog/postgresql-17-incremental-backups/", False),
    ("https://pgpedia.info/p/pg_combinebackup.html", False),
])
def test_search_and_final_fetch_urls_share_official_source_admission(url, allowed):
    config = {"metadata": {"coverage_contract": contract().model_dump(mode="json")}}
    assert source_allowed(url, config) is allowed


@pytest.mark.asyncio
async def test_gateway_failure_survives_governance_as_confirmed_outcome(monkeypatch):
    from open_deep_research.agentscope_runtime.web_tools import (
        WebFetchLedger,
        fetch_url_tool,
    )
    from open_deep_research.sandbox import gateway_tool
    from open_deep_research.sandbox.wire import GatewayToolOutcomeV1
    from open_deep_research.tools.governance import (
        AgentRole,
        execute_governed_tool_call_native,
    )

    config = {"configurable": {"event_log_enabled": False}}
    delegate = fetch_url_tool(lambda: config, None, WebFetchLedger())
    proxy = gateway_tool.GatewayToolProxy(delegate)

    async def failed(*args, **kwargs):
        return GatewayToolOutcomeV1(logical_operation_id="operation", tool_call_id="call",
            status="failed", error={"error_type": "unknown", "message": "Source is outside this run's boundary."})

    monkeypatch.setattr(gateway_tool, "_call_gateway", failed)
    result = await execute_governed_tool_call_native(
        {"name": "fetch_url", "id": "call", "args": {"url": "https://www.postgresql.org/docs/17/"}},
        {"fetch_url": proxy}, AgentRole.RESEARCHER, config,
        allowed_tools={"fetch_url"}, apply_retry=False,
    )
    assert result.confirmed_outcome
    assert result.error.message == "Source is outside this run's boundary."
