"""AgentScope 2.0.8 提供商解析补充；保留 SDK 原始停止原因。"""

from copy import copy
from contextlib import aclosing

from agentscope.model import AnthropicChatModel, GeminiChatModel, DeepSeekChatModel
from open_deep_research.as_runtime.gateway import GovernedModelMixin


class GovernedAnthropicChatModel(GovernedModelMixin, AnthropicChatModel):
    async def _parse_anthropic_completion_response(self, start_datetime, response):
        result = await super()._parse_anthropic_completion_response(
            start_datetime, response
        )
        result.metadata.update(
            provider_finish_reason=response.stop_reason,
            request_id=response.id,
            served_model=response.model,
        )
        self._remember_metadata(result.metadata)
        return result

    async def _parse_anthropic_stream_completion_response(
        self, start_datetime, response
    ):
        metadata = {}

        async def observed(stream):
            async for event in stream:
                if event.type == "message_start":
                    metadata.update(
                        request_id=event.message.id, served_model=event.message.model
                    )
                elif event.type == "message_delta" and event.delta.stop_reason:
                    metadata["provider_finish_reason"] = event.delta.stop_reason
                current = self._call_metadata.get()
                if current is not None:
                    current.update(metadata)
                yield event

        class Stream:
            async def __aenter__(self):
                return observed(await response.__aenter__())

            async def __aexit__(self, *args):
                return await response.__aexit__(*args)

        async with aclosing(
            super()._parse_anthropic_stream_completion_response(
                start_datetime, Stream()
            )
        ) as parsed:
            async for chunk in parsed:
                chunk.metadata.update(metadata)
                yield chunk


class GovernedGeminiChatModel(GovernedModelMixin, GeminiChatModel):
    async def _call_api(
        self, model_name, messages, tools=None, tool_choice=None, **kwargs
    ):
        # 2.0.8 原生实现会用 parameters 覆盖每次调用的输出上限。
        # 为本次调用复制参数，避免改共享模型以及并行调用的上限。
        local = copy(self)
        limit = kwargs.pop(
            "max_tokens", kwargs.pop("max_output_tokens", self.parameters.max_tokens)
        )
        local.parameters = self.parameters.model_copy(update={"max_tokens": limit})
        return self._track_stream(
            await GeminiChatModel._call_api(
                local, model_name, messages, tools, tool_choice, **kwargs
            )
        )

    @staticmethod
    def _metadata(response):
        reason = response.candidates[0].finish_reason if response.candidates else None
        return {
            "provider_finish_reason": getattr(reason, "value", reason),
            "request_id": response.response_id,
            "served_model": response.model_version,
        }

    def _parse_completion_response(self, start_datetime, response):
        result = super()._parse_completion_response(start_datetime, response)
        result.metadata.update(self._metadata(response))
        self._remember_metadata(result.metadata)
        return result

    async def _parse_stream_response(self, start_datetime, response):
        metadata = {}

        async def observed():
            async for raw in response:
                metadata.update(
                    {k: v for k, v in self._metadata(raw).items() if v is not None}
                )
                current = self._call_metadata.get()
                if current is not None:
                    current.update(metadata)
                yield raw

        try:
            async with aclosing(
                super()._parse_stream_response(start_datetime, observed())
            ) as parsed:
                async for chunk in parsed:
                    chunk.metadata.update(metadata)
                    yield chunk
        finally:
            await response.aclose()


class GovernedDeepSeekChatModel(GovernedModelMixin, DeepSeekChatModel):
    def _parse_completion_response(self, start_datetime, response):
        result = super()._parse_completion_response(start_datetime, response)
        result.metadata.update(
            provider_finish_reason=response.choices[0].finish_reason,
            request_id=response.id,
            served_model=response.model,
        )
        self._remember_metadata(result.metadata)
        return result

    async def _parse_stream_response(self, start_datetime, response):
        metadata = {}

        async def observed(stream):
            async for raw in stream:
                metadata.update(request_id=raw.id, served_model=raw.model)
                for choice in raw.choices:
                    if choice.finish_reason is not None:
                        metadata["provider_finish_reason"] = choice.finish_reason
                current = self._call_metadata.get()
                if current is not None:
                    current.update(metadata)
                yield raw

        class Stream:
            async def __aenter__(self):
                return observed(await response.__aenter__())

            async def __aexit__(self, *args):
                return await response.__aexit__(*args)

        async with aclosing(
            super()._parse_stream_response(start_datetime, Stream())
        ) as parsed:
            async for chunk in parsed:
                chunk.metadata.update(metadata)
                yield chunk
