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
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from open_deep_research.agentscope_runtime.models import ModelFactory
from open_deep_research.agentscope_runtime.search_providers import (
    SearchResources,
    SearchService,
    deduplicate_sources,
    parse_anthropic_search,
    parse_openai_search,
)
from open_deep_research.configuration import Configuration
from open_deep_research.prompts import summarize_webpage_prompt
from open_deep_research.tools.availability import (
    provider_search_enabled,
    search_enabled,
)
from open_deep_research.tools.base import (
    ToolOrigin,
    ToolResult,
    build_tool,
)
from open_deep_research.web.models import SearchRequest

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
    """Short, search-engine-ready queries. Begin broad on the first call when
    unconstrained; refine later queries only to address evidence-backed gaps.
    Explicit tool, URL and source restrictions always take precedence.
    """

    queries: list[str] = Field(min_length=1, max_length=24)


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

            try:
                return await current_model.generate_structured_output(
                    [UserMsg("user", prompt)], SummaryOutput
                )
            except asyncio.CancelledError:
                from open_deep_research.agentscope_runtime.gateway import (
                    SandboxChatModel,
                )
                from open_deep_research.agentscope_runtime.recovery_store import (
                    UnknownOperation,
                )

                if isinstance(current_model, SandboxChatModel):
                    raise UnknownOperation("web_summary_outcome_unknown") from None
                raise

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
        except Exception as exc:  # deterministic content fallback only  # noqa: BLE001 - normalize external failures after preserving runtime control
            from open_deep_research.agentscope_runtime.search_providers import (
                preserve_control_error,
            )

            preserve_control_error(exc)
            logger.warning(
                "Summarization failed; external content quarantined: %s", str(exc)[:200]
            )
            return _QUARANTINE_FAILED


async def format_search_batch(batch, summarizer, config):
    """Keep the legacy summary presentation over the shared discovery contract."""
    if not batch.candidates and not batch.syntheses:
        return (
            "Search failed: " + "; ".join(batch.errors) if batch.errors else _NO_RESULTS
        )
    cfg = Configuration.from_runnable_config(config)

    async def summarize(item):
        return (
            await summarizer.summarize(
                item.content_hint[: cfg.max_content_length], config
            )
            if item.content_hint
            else item.snippet
        )

    summaries = await asyncio.gather(*(summarize(item) for item in batch.candidates))
    output = "Search results:\n"
    for index, (item, summary) in enumerate(zip(batch.candidates, summaries), 1):
        output += f"\n\n--- SOURCE {index}: {item.title} ---\nURL: {item.canonical_url}\n\nSUMMARY:\n{summary}\n"
    if batch.syntheses:
        text = "\n\n".join(item.text for item in batch.syntheses)
        output += "\nSUMMARY:\n" + await summarizer.summarize(
            text[: cfg.max_content_length], config
        )
    if batch.errors:
        output += "\nProvider diagnostics: " + "; ".join(batch.errors)
    return output


def _search_tool(
    name,
    run_config_getter,
    factory,
    *,
    resources=None,
    client_factories=None,
    ledger=None,
    browser_tools=(),
    summarizer=None,
):
    from open_deep_research.agentscope_runtime.web_tools import WebFetchLedger

    ledger = ledger or WebFetchLedger()
    summarizer = summarizer or NativeSummarizer(factory)

    async def call(input, context, progress=None):
        from open_deep_research.agentscope_runtime.web_progress import WebProgress
        from open_deep_research.agentscope_runtime.web_shadow import evaluate_shadow
        from open_deep_research.agentscope_runtime.web_tools import execution_config

        config = execution_config(context)
        emitter = progress or WebProgress(
            config,
            task_id=str(config.get("metadata", {}).get("task_id") or "task"),
            tool_call_id=context.tool_call_id,
            operation_id=context.operation_id,
            tool_name=name,
        )
        clients = resources or SearchResources(client_factories)
        try:
            request = SearchRequest(
                objective=" ".join(input.queries),
                queries=input.queries[:3],
                candidate_limit=min(
                    100,
                    max(
                        Configuration.from_runnable_config(
                            config
                        ).search_candidate_limit,
                        5 * len(input.queries),
                    ),
                ),
            )
            request = request.model_copy(update={"queries": input.queries})
            batch = await SearchService(
                config, factory, clients, progress=emitter, legacy=True
            ).discover(request)
            output = await format_search_batch(batch, summarizer, config)
            diagnostics, fetches = await evaluate_shadow(
                config,
                request,
                batch,
                factory,
                clients,
                ledger,
                browser_tools=browser_tools,
                progress=emitter,
            )
            return ToolResult(
                output=output,
                metadata={
                    "physical_fetches": fetches,
                    "transport_failed_fetches": fetches
                    - diagnostics.get("charged_fetch_calls", fetches),
                    "web_diagnostics": {
                        "providers": [p.model_dump() for p in batch.provider_results],
                        "shadow": diagnostics,
                    },
                },
            )
        finally:
            if resources is None:
                await clients.aclose()

    return build_tool(
        name=name,
        description="Search configured web providers in parallel and return cited summaries.",
        input_schema=SearchQueries,
        call=call,
        origin=ToolOrigin.SEARCH,
        concurrency_safe=True,
        retryable=False,
        prompt=f"Use {name} for discovery. Start broad when unconstrained; refine only when the evidence leaves a concrete gap. Search summaries and snippets are discovery hints. Fetch original sources for report evidence.",
        is_enabled=search_enabled,
    )


def tavily_search_tool(run_config_getter, summarizer, *, client_factory=None, **kwargs):
    """Compatibility alias for persisted Tavily calls."""
    return _search_tool(
        "tavily_search",
        run_config_getter,
        getattr(summarizer, "factory", None),
        summarizer=summarizer,
        client_factories={"tavily": client_factory} if client_factory else None,
    )


def openai_web_search_tool(
    run_config_getter, summarizer, *, client_factory=None, **kwargs
):
    """Compatibility alias for persisted OpenAI calls."""
    return _search_tool(
        "openai_web_search",
        run_config_getter,
        getattr(summarizer, "factory", None),
        summarizer=summarizer,
        client_factories={"openai": client_factory} if client_factory else None,
    )


def anthropic_web_search_tool(
    run_config_getter, summarizer, *, client_factory=None, **kwargs
):
    """Compatibility alias for persisted Anthropic calls."""
    return _search_tool(
        "anthropic_web_search",
        run_config_getter,
        getattr(summarizer, "factory", None),
        summarizer=summarizer,
        client_factories={"anthropic": client_factory} if client_factory else None,
    )


def search_provider_tools(
    run_config_getter,
    factory,
    *,
    client_factories=None,
    resources=None,
    ledger=None,
    browser_tools=(),
):
    """Expose one unified tool on new runs; preserve old frozen tool names."""
    config = run_config_getter()
    cfg = Configuration.from_runnable_config(config)
    if not cfg.resolved_search_providers:
        return []
    version = config.get("metadata", {}).get("run_config_schema_version", 16)
    name = (
        {
            "tavily": "tavily_search",
            "openai": "openai_web_search",
            "anthropic": "anthropic_web_search",
        }.get(cfg.search_api.value, "web_search")
        if version < 16
        else "web_search"
    )
    return [
        _search_tool(
            name,
            run_config_getter,
            factory,
            resources=resources,
            client_factories=client_factories,
            ledger=ledger,
            browser_tools=browser_tools,
        )
    ]


__all__ = [
    "NativeSummarizer",
    "SearchQueries",
    "SummaryOutput",
    "deduplicate_sources",
    "parse_anthropic_search",
    "parse_openai_search",
    "provider_search_enabled",
    "search_provider_tools",
]
