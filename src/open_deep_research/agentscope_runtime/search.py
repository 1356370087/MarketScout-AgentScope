"""原生搜索提供商与摘要调用（T029）。

在 AgentScope 运行时中提供四个 ``search_api`` 分支（tavily/openai/anthropic/
none）的搜索工具，不依赖 LangChain：

- 并行多查询搜索（``asyncio.gather``）、按 URL 去重保留首见顺序、输出格式
  与旧实现一致。
- 摘要走原生模型栈（``ModelFactory`` 的 summarization 角色 + 候选链策略），
  保持 120 秒预算与 fail-closed 隔离：超时/失败输出隔离占位符，外部内容
  永不原样返回给模型。
- 提供商 SDK 客户端 ``max_retries=0``：重试所有者是工具治理层，不在 SDK
  内叠加。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from open_deep_research.agentscope_runtime.models import ModelFactory
from open_deep_research.configuration import Configuration, SearchAPI
from open_deep_research.prompts import summarize_webpage_prompt
from open_deep_research.sandbox.policy import network_policy_mode
from open_deep_research.tools.base import (
    Tool,
    ToolContext,
    ToolOrigin,
    ToolResult,
    build_tool,
)

logger = logging.getLogger(__name__)

SUMMARY_TIMEOUT_SECONDS = 120.0
_QUARANTINE_TIMEOUT = '<external_content_quarantined reason="summarization_timeout"/>'
_QUARANTINE_FAILED = '<external_content_quarantined reason="summarization_failed"/>'
_NO_RESULTS = (
    "No valid search results found. Please try different search queries "
    "or use a different search API."
)


class SummaryOutput(BaseModel):
    """结构化摘要输出（与旧 Summary 契约一致）。"""

    summary: str
    key_excerpts: str


class SearchQueries(BaseModel):
    """搜索工具的模型输入；max_results/topic 由运行时注入。"""

    queries: list[str] = Field(min_length=1)


def provider_search_enabled(config: dict[str, Any], search_api: SearchAPI) -> bool:
    """原生可用性谓词：legacy/shadow 模式 + 分支匹配 + 非离线网络。"""
    configurable = Configuration.from_runnable_config(config)
    if configurable.web_pipeline_mode == "enforced":
        return False
    if configurable.search_api != search_api:
        return False
    return network_policy_mode(configurable) != "offline"


class NativeSummarizer:
    """原生摘要调用：候选链结构化输出 + 120 秒预算 + fail-closed 隔离。"""

    def __init__(self, factory: ModelFactory) -> None:
        self.factory = factory

    async def summarize(
        self,
        content: str,
        config: dict[str, Any] | None = None,
        *,
        timeout: float = SUMMARY_TIMEOUT_SECONDS,
    ) -> str:
        prompt = summarize_webpage_prompt.format(
            webpage_content=content,
            date=datetime.now(UTC).date().isoformat(),
        )

        async def handler(current_model: Any, messages: Any, **_: Any):
            from agentscope.message import UserMsg

            return await current_model.generate_structured_output(
                [UserMsg("user", prompt)], SummaryOutput
            )

        try:
            middleware = self.factory.policy_middleware("summarization")
            result = await asyncio.wait_for(
                middleware.policy.invoke(handler, {"messages": [prompt]}, {}),
                timeout=timeout,
            )
            return (
                f"<summary>\n{result.content['summary']}\n</summary>\n\n"
                f"<key_excerpts>\n{result.content['key_excerpts']}\n</key_excerpts>"
            )
        except TimeoutError:
            logger.warning(
                "Summarization timed out after %s seconds; content quarantined", timeout
            )
            return _QUARANTINE_TIMEOUT
        except Exception as exc:  # noqa: BLE001 - external content remains quarantined
            logger.warning(
                "Summarization failed; external content quarantined: %s", str(exc)[:200]
            )
            return _QUARANTINE_FAILED


def parse_openai_search(response: Any) -> tuple[str, list[dict[str, str]]]:
    """Extract synthesized text and URL citations from OpenAI Responses."""
    text = str(getattr(response, "output_text", "") or "")
    sources: list[dict[str, str]] = []
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) != "message":
            continue
        for part in getattr(item, "content", None) or []:
            for annotation in getattr(part, "annotations", None) or []:
                url = getattr(annotation, "url", None)
                if url:
                    sources.append(
                        {
                            "url": str(url),
                            "title": str(getattr(annotation, "title", None) or url),
                        }
                    )
    return text, sources


def parse_anthropic_search(response: Any) -> tuple[str, list[dict[str, str]]]:
    """Extract synthesized text and sources from Anthropic web-search blocks."""
    text_parts: list[str] = []
    sources: list[dict[str, str]] = []
    for block in getattr(response, "content", None) or []:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text_parts.append(str(getattr(block, "text", "") or ""))
        elif block_type == "web_search_tool_result":
            for result in getattr(block, "content", None) or []:
                url = getattr(result, "url", None)
                if url:
                    sources.append(
                        {
                            "url": str(url),
                            "title": str(getattr(result, "title", None) or url),
                        }
                    )
    return "\n".join(text_parts), sources


def deduplicate_sources(sources: list[dict[str, str]]) -> list[dict[str, str]]:
    """Deduplicate sources by URL while preserving discovery order."""
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for source in sources:
        if source["url"] not in seen:
            seen.add(source["url"])
            unique.append(source)
    return unique


def _build_openai_client(config: dict[str, Any]):
    import httpx
    from openai import AsyncOpenAI

    from open_deep_research.models.resolution import resolve_named_api_key

    configurable = Configuration.from_runnable_config(config)
    return AsyncOpenAI(
        api_key=resolve_named_api_key("OPENAI_API_KEY", config),
        timeout=httpx.Timeout(60.0),
        max_retries=0,
        base_url=configurable.openai_base_url or None,
    )


def _build_anthropic_client(config: dict[str, Any]):
    import httpx
    from anthropic import AsyncAnthropic

    from open_deep_research.models.resolution import resolve_named_api_key

    return AsyncAnthropic(
        api_key=resolve_named_api_key("ANTHROPIC_API_KEY", config),
        timeout=httpx.Timeout(60.0),
        max_retries=0,
    )


def _build_tavily_client(config: dict[str, Any]):
    from tavily import AsyncTavilyClient

    from open_deep_research.models.resolution import resolve_named_api_key

    return AsyncTavilyClient(api_key=resolve_named_api_key("TAVILY_API_KEY", config))


async def _format_synthesized_search(
    summarizer: NativeSummarizer,
    synthesized_text: str,
    sources: list[dict[str, str]],
    config: dict[str, Any],
) -> str:
    if not sources and not synthesized_text.strip():
        return _NO_RESULTS
    configurable = Configuration.from_runnable_config(config)
    summary = await summarizer.summarize(
        synthesized_text[: configurable.max_content_length] if synthesized_text else "",
        config,
    )
    output = "Search results: \n"
    for index, source in enumerate(sources, 1):
        output += (
            f"\n\n--- SOURCE {index}: {source['title']} ---\n" f"URL: {source['url']}\n"
        )
    return output + f"\n\nSUMMARY:\n{summary}\n\n" + ("-" * 80) + "\n"


def tavily_search_tool(
    run_config_getter: Callable[[], dict[str, Any]],
    summarizer: NativeSummarizer,
    *,
    client_factory: Callable[[dict[str, Any]], Any] = _build_tavily_client,
    description: str = "Search the web with Tavily and return a multi-source research digest.",
    prompt: str = (
        "Use tavily_search in legacy or shadow web pipeline modes. Start broad, "
        "inspect the returned sources, then refine only when the evidence leaves "
        "a concrete gap."
    ),
) -> Tool:
    """并行搜索 → URL 去重 → 原生摘要隔离的 Tavily 工具。"""

    async def _no_summary() -> None:
        return None

    async def call(input: SearchQueries, context: ToolContext, progress=None):
        config = context.config
        client = client_factory(config)
        responses = await asyncio.gather(
            *[
                client.search(
                    query,
                    max_results=5,
                    topic="general",
                    include_raw_content=True,
                )
                for query in input.queries
            ]
        )
        unique_results: dict[str, dict] = {}
        for response in responses:
            for result in response["results"]:
                unique_results.setdefault(
                    result["url"], {**result, "query": response["query"]}
                )
        configurable = Configuration.from_runnable_config(config)
        summaries = await asyncio.gather(
            *[
                summarizer.summarize(
                    result["raw_content"][: configurable.max_content_length],
                    config,
                )
                if result.get("raw_content")
                else _no_summary()
                for result in unique_results.values()
            ]
        )
        if not unique_results:
            return ToolResult(output=_NO_RESULTS)
        output = "Search results: \n\n"
        for index, ((url, result), summary) in enumerate(
            zip(unique_results.items(), summaries), 1
        ):
            content = result.get("content", "") if summary is None else summary
            output += f"\n\n--- SOURCE {index}: {result['title']} ---\n"
            output += f"URL: {url}\n\nSUMMARY:\n{content}\n\n"
            output += "\n\n" + "-" * 80 + "\n"
        return ToolResult(output=output)

    return build_tool(
        name="tavily_search",
        description=description,
        input_schema=SearchQueries,
        call=call,
        origin=ToolOrigin.SEARCH,
        retryable=True,
        concurrency_safe=True,
        prompt=prompt,
        is_enabled=lambda config: provider_search_enabled(config, SearchAPI.TAVILY),
    )


def openai_web_search_tool(
    run_config_getter: Callable[[], dict[str, Any]],
    summarizer: NativeSummarizer,
    *,
    client_factory: Callable[[dict[str, Any]], Any] = _build_openai_client,
    description: str = "Search the web using OpenAI native web search.",
    prompt: str = (
        "Use openai_web_search in legacy or shadow web pipeline modes. The tool "
        "runs server-side web search and returns a cited digest."
    ),
) -> Tool:
    """OpenAI Responses 服务端搜索 → URL 去重 → 摘要格式化。"""

    async def call(input: SearchQueries, context: ToolContext, progress=None):
        config = context.config
        configurable = Configuration.from_runnable_config(config)
        client = client_factory(config)
        model = configurable.research_model
        if model and ":" in model and model.split(":", 1)[0] == "openai":
            model = model.split(":", 1)[1]
        responses = await asyncio.gather(
            *[
                client.responses.create(
                    model=model,
                    input=query,
                    tools=[{"type": "web_search_preview"}],
                )
                for query in input.queries
            ]
        )
        text_parts: list[str] = []
        all_sources: list[dict[str, str]] = []
        for response in responses:
            text, sources = parse_openai_search(response)
            if text:
                text_parts.append(text)
            all_sources.extend(sources)
        synthesized = "\n\n".join(text_parts)
        capped = deduplicate_sources(all_sources)[: 5 * max(1, len(input.queries))]
        return ToolResult(
            output=await _format_synthesized_search(
                summarizer, synthesized, capped, config
            )
        )

    return build_tool(
        name="openai_web_search",
        description=description,
        input_schema=SearchQueries,
        call=call,
        origin=ToolOrigin.SEARCH,
        retryable=True,
        concurrency_safe=True,
        prompt=prompt,
        is_enabled=lambda config: provider_search_enabled(config, SearchAPI.OPENAI),
    )


def anthropic_web_search_tool(
    run_config_getter: Callable[[], dict[str, Any]],
    summarizer: NativeSummarizer,
    *,
    client_factory: Callable[[dict[str, Any]], Any] = _build_anthropic_client,
    description: str = "Search the web using Anthropic native web search.",
    prompt: str = (
        "Use anthropic_web_search in legacy or shadow web pipeline modes. The "
        "tool runs server-side web search and returns a cited digest."
    ),
) -> Tool:
    """Anthropic 服务端 web_search 工具 → URL 去重 → 摘要格式化。"""

    async def call(input: SearchQueries, context: ToolContext, progress=None):
        config = context.config
        configurable = Configuration.from_runnable_config(config)
        client = client_factory(config)
        model = configurable.research_model
        if model and ":" in model and model.split(":", 1)[0] == "anthropic":
            model = model.split(":", 1)[1]
        responses = await asyncio.gather(
            *[
                client.messages.create(
                    model=model,
                    max_tokens=configurable.research_model_max_tokens,
                    messages=[{"role": "user", "content": query}],
                    tools=[
                        {
                            "type": "web_search_20250305",
                            "name": "web_search",
                            "max_uses": 5,
                        }
                    ],
                )
                for query in input.queries
            ]
        )
        text_parts: list[str] = []
        all_sources: list[dict[str, str]] = []
        for response in responses:
            text, sources = parse_anthropic_search(response)
            if text:
                text_parts.append(text)
            all_sources.extend(sources)
        synthesized = "\n\n".join(text_parts)
        capped = deduplicate_sources(all_sources)[: 5 * max(1, len(input.queries))]
        return ToolResult(
            output=await _format_synthesized_search(
                summarizer, synthesized, capped, config
            )
        )

    return build_tool(
        name="anthropic_web_search",
        description=description,
        input_schema=SearchQueries,
        call=call,
        origin=ToolOrigin.SEARCH,
        retryable=True,
        concurrency_safe=True,
        prompt=prompt,
        is_enabled=lambda config: provider_search_enabled(config, SearchAPI.ANTHROPIC),
    )


def search_provider_tools(
    run_config_getter: Callable[[], dict[str, Any]],
    factory: ModelFactory,
    *,
    client_factories: dict[str, Callable[[dict[str, Any]], Any]] | None = None,
) -> list[Tool]:
    """按 ``search_api`` 分支返回当前可用的原生搜索工具（none 返回空）。"""
    summarizer = NativeSummarizer(factory)
    client_factories = client_factories or {}
    config = run_config_getter()
    search_api = Configuration.from_runnable_config(config).search_api
    builders = {
        SearchAPI.TAVILY: tavily_search_tool,
        SearchAPI.OPENAI: openai_web_search_tool,
        SearchAPI.ANTHROPIC: anthropic_web_search_tool,
    }
    builder = builders.get(search_api)
    if builder is None:
        return []
    factory_key = {
        SearchAPI.TAVILY: "tavily",
        SearchAPI.OPENAI: "openai",
        SearchAPI.ANTHROPIC: "anthropic",
    }[search_api]
    return [
        builder(
            run_config_getter,
            summarizer,
            client_factory=client_factories.get(factory_key)
            or {
                "tavily": _build_tavily_client,
                "openai": _build_openai_client,
                "anthropic": _build_anthropic_client,
            }[factory_key],
        )
    ]


__all__ = [
    "NativeSummarizer",
    "SearchQueries",
    "SummaryOutput",
    "anthropic_web_search_tool",
    "deduplicate_sources",
    "openai_web_search_tool",
    "parse_anthropic_search",
    "parse_openai_search",
    "provider_search_enabled",
    "search_provider_tools",
    "tavily_search_tool",
]
