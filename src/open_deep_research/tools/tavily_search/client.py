"""Tavily API client helpers."""

from __future__ import annotations

import asyncio
from typing import Literal

from langchain_core.runnables import RunnableConfig
from tavily import AsyncTavilyClient  # type: ignore[import-untyped]

from open_deep_research.models.resolution import resolve_named_api_key


def get_tavily_api_key(config: RunnableConfig) -> str | None:
    """Resolve Tavily credentials through the shared execution-zone policy."""
    return resolve_named_api_key("TAVILY_API_KEY", config)


async def tavily_search_async(
    search_queries: list[str],
    max_results: int = 5,
    topic: Literal["general", "news", "finance"] = "general",
    include_raw_content: bool = True,
    config: RunnableConfig = None,
) -> list[dict]:
    """Execute multiple Tavily queries concurrently."""
    client = AsyncTavilyClient(api_key=get_tavily_api_key(config))
    return await asyncio.gather(
        *[
            client.search(
                query,
                max_results=max_results,
                include_raw_content=include_raw_content,
                topic=topic,
            )
            for query in search_queries
        ]
    )


__all__ = ["get_tavily_api_key", "tavily_search_async"]
