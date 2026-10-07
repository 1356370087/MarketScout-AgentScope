"""AgentScope native server-search model port with structured citations and usage."""

from __future__ import annotations

import json
import time

from agentscope.credential import CredentialBase
from agentscope.model import ChatModelBase, ChatUsage, StructuredResponse
from pydantic import BaseModel

from open_deep_research.agentscope_runtime.search_providers import (
    parse_anthropic_search,
    parse_openai_search,
)


class ServerSearchModel(ChatModelBase):
    """Use provider server tools while returning the native model result contract."""

    class Parameters(BaseModel):
        max_tokens: int = 4096

    def __init__(
        self,
        *,
        provider,
        model,
        api_key,
        base_url=None,
        http_client=None,
        max_tokens=4096,
        proxy=False,
    ):
        super().__init__(
            CredentialBase(),
            model,
            self.Parameters(max_tokens=max_tokens),
            stream=False,
            max_retries=0,
        )
        self.provider = provider
        self.proxy = proxy
        if provider == "openai":
            from openai import AsyncOpenAI

            self.client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                http_client=http_client,
                max_retries=0,
                timeout=60,
            )
        else:
            from anthropic import AsyncAnthropic

            self.client = AsyncAnthropic(
                api_key=api_key,
                base_url=base_url.rstrip("/").removesuffix("/v1") if base_url else None,
                http_client=http_client,
                max_retries=0,
                timeout=60,
            )

    async def _call_api(self, *args, **kwargs):
        raise ValueError("server search uses the search_web port")

    async def search_web(
        self,
        query,
        *,
        allowed_domains=(),
        blocked_domains=(),
        progress=None,
        logical_operation_id=None,
        trace_metadata=None,
    ):
        """Stream provider events, preserving usage on successful search results."""
        started = time.monotonic()
        model = self.model if self.proxy else self.model.split(":", 1)[-1]
        headers = (
            {"x-litellm-request-id": logical_operation_id}
            if logical_operation_id
            else None
        )
        if self.provider == "openai":
            tool = {"type": "web_search_preview"}
            if allowed_domains:
                tool = {
                    "type": "web_search",
                    "filters": {"allowed_domains": list(allowed_domains)},
                }
            async with self.client.responses.stream(
                model=model,
                input=query,
                tools=[tool],
                max_output_tokens=self.parameters.max_tokens,
                extra_headers=headers,
            ) as stream:
                async for event in stream:
                    if progress and event.type == "response.web_search_call.searching":
                        await progress(
                            "server_searching", provider=self.provider, query=query
                        )
                response = await stream.get_final_response()
            text, sources = parse_openai_search(response)
            usage = response.usage
            cached = (
                getattr(
                    getattr(usage, "input_tokens_details", None), "cached_tokens", 0
                )
                or 0
            )
            finish = "length" if response.status == "incomplete" else "stop"
        else:
            tool = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}
            if allowed_domains:
                tool["allowed_domains"] = list(allowed_domains)
            elif blocked_domains:
                tool["blocked_domains"] = list(blocked_domains)
            partial = {}
            async with self.client.messages.stream(
                model=model,
                max_tokens=self.parameters.max_tokens,
                messages=[{"role": "user", "content": query}],
                tools=[tool],
                extra_headers=headers,
            ) as stream:
                async for event in stream:
                    if (
                        event.type == "content_block_start"
                        and event.content_block.type == "server_tool_use"
                    ):
                        partial[event.index] = ""
                    elif (
                        event.type == "content_block_delta"
                        and event.index in partial
                        and event.delta.type == "input_json_delta"
                    ):
                        partial[event.index] += event.delta.partial_json
                    elif event.type == "content_block_stop" and event.index in partial:
                        try:
                            actual_query = json.loads(partial.pop(event.index)).get(
                                "query"
                            )
                        except ValueError, TypeError:
                            actual_query = None
                        if actual_query and progress:
                            await progress(
                                "server_query",
                                provider=self.provider,
                                query=actual_query,
                            )
                    elif (
                        event.type == "content_block_start"
                        and event.content_block.type == "web_search_tool_result"
                        and isinstance(event.content_block.content, list)
                        and progress
                    ):
                        await progress(
                            "server_results",
                            provider=self.provider,
                            query=query,
                            query_index=event.index,
                            result_count=len(event.content_block.content),
                        )
                response = await stream.get_final_message()
            try:
                text, sources = parse_anthropic_search(response)
            except Exception as exc:
                exc.usage = response.usage
                raise
            usage = response.usage
            cached = getattr(usage, "cache_read_input_tokens", 0) or 0
            finish = response.stop_reason
        raw_usage = (
            {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cached_input_tokens": cached,
            }
            if usage
            else {}
        )
        return StructuredResponse(
            content={"text": text, "sources": sources},
            usage=ChatUsage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_input_tokens=cached,
                time=time.monotonic() - started,
            )
            if usage
            else None,
            metadata={
                "request_id": response.id,
                "served_model": response.model,
                "provider_finish_reason": finish,
                "raw_usage": raw_usage,
            },
        )


async def invoke_search(factory, provider, query, **options):
    """Invoke the search model through the shared native execution policy."""
    from agentscope.message import UserMsg

    async def handler(current_model, messages, **kwargs):
        return await current_model.search_web(query, **options)

    result = await factory.policy_middleware(f"{provider}_search").policy.invoke(
        handler,
        {"messages": [UserMsg("user", query)]},
        {},
    )
    return {**result.content, "usage": result.metadata.get("raw_usage", {})}
