"""AS-A029: 原生搜索四分支、并发、去重与摘要超时隔离。"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from open_deep_research.agentscope_runtime.models import ModelFactory
from open_deep_research.agentscope_runtime.search import (
    NativeSummarizer,
    deduplicate_sources,
    parse_anthropic_search,
    parse_openai_search,
    provider_search_enabled,
    search_provider_tools,
)
from open_deep_research.configuration import SearchAPI
from open_deep_research.tools.base import ToolContext

pytestmark = pytest.mark.asyncio


def _config(**configurable):
    return {
        "configurable": {"event_log_enabled": False, **configurable},
        "metadata": {},
    }


def _ctx(cfg):
    return ToolContext(config=cfg, role="researcher", tool_call_id="t", operation_id="op")


# ------------------------------------------------------------ 四个分支


async def test_four_search_api_branches():
    def make(api):
        return lambda: _config(search_api=api)

    for api, expected in [
        (SearchAPI.TAVILY, "tavily_search"),
        (SearchAPI.OPENAI, "openai_web_search"),
        (SearchAPI.ANTHROPIC, "anthropic_web_search"),
    ]:
        tools = search_provider_tools(
            make(api), _StubFactory(), client_factories=_FAKE_CLIENTS
        )
        assert [tool.name for tool in tools] == [expected]
    assert search_provider_tools(make(SearchAPI.NONE), _StubFactory()) == []

    # enforced 模式下传统搜索分支全部禁用（只有 web_research 管线）。
    enforced = _config(search_api=SearchAPI.TAVILY, web_pipeline_mode="enforced")
    assert provider_search_enabled(enforced, SearchAPI.TAVILY) is False
    legacy = _config(search_api=SearchAPI.TAVILY, web_pipeline_mode="legacy")
    assert provider_search_enabled(legacy, SearchAPI.TAVILY) is True
    shadow = _config(search_api=SearchAPI.OPENAI, web_pipeline_mode="shadow")
    assert provider_search_enabled(shadow, SearchAPI.OPENAI) is True
    # 分支不匹配时其它分支的工具不可用。
    assert provider_search_enabled(legacy, SearchAPI.OPENAI) is False


async def test_offline_network_disables_search(monkeypatch):
    import open_deep_research.agentscope_runtime.search as search_mod

    monkeypatch.setattr(search_mod, "network_policy_mode", lambda configurable: "offline")
    cfg = _config(search_api=SearchAPI.TAVILY, web_pipeline_mode="legacy")
    assert provider_search_enabled(cfg, SearchAPI.TAVILY) is False


# --------------------------------------------------------- tavily 行为


class _StubFactory(ModelFactory):
    """绕过真实凭据绑定；策略中间件按需注入。"""

    def __init__(self):
        pass


class _StubPolicy:
    def __init__(self, behavior):
        self.behavior = behavior

    async def invoke(self, handler, input_kwargs, state):
        return await self.behavior(handler, input_kwargs, state)


class _StubMiddleware:
    def __init__(self, behavior):
        self.policy = _StubPolicy(behavior)


class _FakeTavily:
    def __init__(self, results_by_query):
        self.results_by_query = results_by_query
        self.active = 0
        self.max_active = 0

    async def search(self, query, **kwargs):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        assert kwargs["include_raw_content"] is True
        try:
            await asyncio.sleep(0.05)
            return {"query": query, "results": self.results_by_query[query]}
        finally:
            self.active -= 1


async def _fixed_summary(self, content, config, **_):
    return f"<summary>S:{len(content)}</summary>\n\n<key_excerpts>k</key_excerpts>"


async def test_tavily_parallel_dedup_and_format(monkeypatch):
    monkeypatch.setattr(NativeSummarizer, "summarize", _fixed_summary)
    tavily = _FakeTavily(
        {
            "q1": [
                {"url": "https://a.example/1", "title": "A1", "content": "snippet-a1",
                 "raw_content": "x" * 100},
                {"url": "https://b.example/2", "title": "B2", "content": "snippet-b2",
                 "raw_content": "y" * 100},
            ],
            "q2": [
                # 与 q1 重复的 URL：去重保留首见（query 归属 q1）。
                {"url": "https://a.example/1", "title": "A1-dup", "content": "dup",
                 "raw_content": "z" * 50},
                {"url": "https://c.example/3", "title": "C3", "content": "snippet-c3",
                 "raw_content": None},
            ],
        }
    )
    cfg = _config(search_api=SearchAPI.TAVILY, web_pipeline_mode="legacy")
    tools = search_provider_tools(
        lambda: cfg, _StubFactory(), client_factories={"tavily": lambda _: tavily}
    )
    tool = tools[0]
    result = await tool.call(
        tool.input_schema.model_validate({"queries": ["q1", "q2"]}), _ctx(cfg)
    )
    # 两个查询并行执行。
    assert tavily.max_active == 2
    output = result.output
    # 去重后恰三个来源，且保留首见标题。
    assert output.count("--- SOURCE") == 3
    assert "A1" in output and "A1-dup" not in output
    assert "URL: https://a.example/1" in output
    assert "URL: https://b.example/2" in output
    assert "URL: https://c.example/3" in output
    # raw_content 走摘要；无 raw_content 回退 provider snippet。
    assert "<summary>S:100</summary>" in output
    assert "SUMMARY:\nsnippet-c3" in output


# --------------------------------------------------- openai/anthropic 行为


def _openai_response(text, annotations):
    return SimpleNamespace(
        output_text=text,
        output=[
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(annotations=annotations)],
            )
        ],
    )


def _anthropic_response(blocks):
    return SimpleNamespace(content=blocks)


async def test_openai_parallel_dedup_cap_and_format(monkeypatch):
    monkeypatch.setattr(NativeSummarizer, "summarize", _fixed_summary)
    calls = []

    class FakeResponses:
        async def create(self, *, model, input, tools):
            calls.append((model, input))
            await asyncio.sleep(0.02)
            return _openai_response(
                f"synth-{input}",
                [
                    SimpleNamespace(url=f"https://x.example/{input}", title=input),
                    # 跨查询重复 URL。
                    SimpleNamespace(url="https://dup.example/x", title="dup"),
                ],
            )

    class FakeClient:
        def __init__(self):
            self.responses = FakeResponses()

    cfg = _config(
        search_api=SearchAPI.OPENAI,
        web_pipeline_mode="shadow",
        research_model="openai:gpt-test",
    )
    tools = search_provider_tools(
        lambda: cfg,
        _StubFactory(),
        client_factories={"openai": lambda _: FakeClient()},
    )
    tool = tools[0]
    queries = [f"q{i}" for i in range(7)]
    result = await tool.call(tool.input_schema.model_validate({"queries": queries}), _ctx(cfg))
    # 全部查询并发发起；provider 前缀被剥离。
    assert len(calls) == 7 and {model for model, _ in calls} == {"gpt-test"}
    output = result.output
    # 去重后：7 个独立 URL + 1 个重复 = 8；上限 5*7 不触发。
    assert output.count("--- SOURCE") == 8
    assert "<summary>S:" in output  # 合成文本经过摘要


async def test_openai_source_cap_matches_baseline():
    sources = [{"url": f"https://u.example/{i}", "title": str(i)} for i in range(30)]
    capped = deduplicate_sources(sources + sources)[: 5 * 2]
    assert len(capped) == 10


class _FakeAnthropicMessages:
    def __init__(self):
        self.records = []
        self.tool_configs = []

    async def create(self, *, model, max_tokens, messages, tools):
        self.records.append((model, max_tokens))
        self.tool_configs.append(tools)
        query = messages[0]["content"]
        return _anthropic_response(
            [
                SimpleNamespace(type="text", text=f"answer-{query}"),
                SimpleNamespace(
                    type="web_search_tool_result",
                    content=[
                        SimpleNamespace(url=f"https://y.example/{query}", title=query),
                        SimpleNamespace(url="https://shared.example/s", title="shared"),
                    ],
                ),
            ]
        )


async def test_anthropic_parse_dedup_and_format(monkeypatch):
    monkeypatch.setattr(NativeSummarizer, "summarize", _fixed_summary)
    messages_client = _FakeAnthropicMessages()

    class FakeClient:
        def __init__(self):
            self.messages = messages_client

    cfg = _config(
        search_api=SearchAPI.ANTHROPIC,
        web_pipeline_mode="legacy",
        research_model="anthropic:claude-test",
        research_model_max_tokens=1234,
    )
    tools = search_provider_tools(
        lambda: cfg,
        _StubFactory(),
        client_factories={"anthropic": lambda _: FakeClient()},
    )
    tool = tools[0]
    result = await tool.call(
        tool.input_schema.model_validate({"queries": ["a", "b"]}), _ctx(cfg)
    )
    assert messages_client.records == [("claude-test", 1234)] * 2
    assert all(
        tool_config[0]["type"] == "web_search_20250305"
        for tool_config in messages_client.tool_configs
    )
    assert result.output.count("--- SOURCE") == 3  # a, b, shared（去重）


async def test_no_results_message(monkeypatch):
    monkeypatch.setattr(NativeSummarizer, "summarize", _fixed_summary)
    empty = _FakeTavily({"q": []})
    cfg = _config(search_api=SearchAPI.TAVILY, web_pipeline_mode="legacy")
    tools = search_provider_tools(
        lambda: cfg, _StubFactory(), client_factories={"tavily": lambda _: empty}
    )
    tool = tools[0]
    result = await tool.call(tool.input_schema.model_validate({"queries": ["q"]}), _ctx(cfg))
    assert result.output.startswith("No valid search results found")


# ------------------------------------------------------- 摘要超时与隔离


async def test_summary_timeout_and_failure_quarantine():
    class SleepyFactory(_StubFactory):
        def policy_middleware(self, role, candidates=None):
            async def behavior(handler, input_kwargs, state):
                await asyncio.sleep(5)

            return _StubMiddleware(behavior)

    started = time.monotonic()
    output = await NativeSummarizer(SleepyFactory()).summarize(
        "content", _config(), timeout=0.05
    )
    assert time.monotonic() - started < 2
    assert output == '<external_content_quarantined reason="summarization_timeout"/>'

    class FailingFactory(_StubFactory):
        def policy_middleware(self, role, candidates=None):
            async def behavior(handler, input_kwargs, state):
                raise RuntimeError("provider down")

            return _StubMiddleware(behavior)

    output = await NativeSummarizer(FailingFactory()).summarize("content", _config())
    assert output == '<external_content_quarantined reason="summarization_failed"/>'


async def test_summary_success_via_structured_output():
    from agentscope.model import StructuredResponse

    class FakeModel:
        async def generate_structured_output(self, messages, schema):
            assert schema.model_json_schema()["properties"]  # SummaryOutput 契约
            return StructuredResponse(
                content={"summary": "s-body", "key_excerpts": "k-body"}
            )

    class OkFactory(_StubFactory):
        def policy_middleware(self, role, candidates=None):
            async def behavior(handler, input_kwargs, state):
                return await handler(current_model=FakeModel(), messages=[])

            return _StubMiddleware(behavior)

    output = await NativeSummarizer(OkFactory()).summarize("content", _config())
    assert output == (
        "<summary>\ns-body\n</summary>\n\n<key_excerpts>\nk-body\n</key_excerpts>"
    )


_FAKE_CLIENTS: dict = {}


def _parse_helpers_pure():
    response = _openai_response("t", [SimpleNamespace(url="https://u", title="n")])
    assert parse_openai_search(response) == ("t", [{"url": "https://u", "title": "n"}])
    blocks = [
        SimpleNamespace(type="text", text="hello"),
        SimpleNamespace(
            type="web_search_tool_result",
            content=[SimpleNamespace(url="https://v", title="m")],
        ),
    ]
    assert parse_anthropic_search(_anthropic_response(blocks)) == (
        "hello",
        [{"url": "https://v", "title": "m"}],
    )


def test_parse_helpers():
    _parse_helpers_pure()
