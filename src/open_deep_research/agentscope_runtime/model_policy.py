"""原生模型调用策略：有限候选链、真实首包、熔断与恢复（T021/T022）。"""

from __future__ import annotations
import asyncio
import time
from copy import deepcopy
from dataclasses import asdict
from agentscope.middleware import MiddlewareBase
from agentscope.message import Msg, TextBlock, ThinkingBlock, UserMsg
from agentscope.model import ChatResponse, StructuredResponse, FinishedReason
from open_deep_research.models.circuit import (
    ModelCircuitBreaker,
    ModelCircuitPolicy,
    CircuitFailureKind,
    CircuitOpenError,
)
from open_deep_research.models.errors import gateway_error_indicates_token_limit


class RecoveryExhausted(RuntimeError):
    pass


def retryable(error):
    if getattr(error, "uncertain", False):
        return False
    status = getattr(error, "status_code", None)
    # google-genai 的 APIError 使用整数 code，未设置 status_code。
    if status is None and isinstance(getattr(error, "code", None), int):
        status = error.code
    if status is None and getattr(error, "response", None) is not None:
        status = error.response.status_code
    if status in {401, 403, 400, 422}:
        return False
    if status == 404:
        return getattr(error, "code", None) in {"model_not_found", "model_unavailable"}
    return (
        status in {408, 429, 500, 502, 503, 504}
        or isinstance(error, (TimeoutError, ConnectionError))
        or type(error).__name__
        in {"APIConnectionError", "APITimeoutError", "ConnectError", "ReadError"}
    )


def safe_fallback_messages(messages):
    """新提供商不能复用签名思考块；正文、媒体与工具调用/结果保留。"""
    result = []
    for msg in messages:
        copied = msg.model_copy(deep=True)
        copied.content = [b for b in copied.content if not isinstance(b, ThinkingBlock)]
        result.append(copied)
    return result


class ModelCallPolicy:
    def __init__(
        self,
        candidates,
        *,
        attempts=3,
        first_packet_timeout=30.0,
        circuit_policy=None,
        circuit_enabled=True,
        probe_mode="enforced",
    ):
        if not candidates or attempts < 1 or first_packet_timeout <= 0:
            raise ValueError("invalid model call policy")
        self.candidates = tuple(candidates)
        self.attempts = attempts
        self.timeout = first_packet_timeout
        self.probe_mode = probe_mode
        self.breakers = (
            [
                ModelCircuitBreaker(
                    model_id=str(i), policy=circuit_policy or ModelCircuitPolicy()
                )
                for i in range(len(candidates))
            ]
            if circuit_enabled
            else [None] * len(candidates)
        )

    async def invoke(self, handler, input_kwargs, state):
        """状态由 AgentState.middle_context 持久化；不在策略实例共享候选游标。"""
        index = state.setdefault("active_candidate_index", 0)
        if not isinstance(index, int) or not 0 <= index < len(self.candidates):
            raise ValueError("invalid restored model candidate index")
        original = input_kwargs["messages"]
        last_error = None
        for index in range(index, len(self.candidates)):
            model = self.candidates[index]
            state["active_candidate_index"] = index
            kwargs = {
                **input_kwargs,
                "current_model": model,
                "messages": safe_fallback_messages(original) if index else original,
            }
            # 沙箱服务拥有其内部重试和账本；客户端不重放结果未知的操作。
            from open_deep_research.agentscope_runtime.gateway import SandboxChatModel

            gateway_owned = getattr(model, "retry_owner", None) == "gateway"
            budget = (
                1
                if gateway_owned or isinstance(model, SandboxChatModel)
                else self.attempts
            )
            for _ in range(budget):
                breaker = self.breakers[index]
                permit = None
                stream = None
                start = time.monotonic()
                try:
                    if breaker:
                        permit, _ = await breaker.before_call()
                    async with asyncio.timeout(
                        self.timeout if self.probe_mode == "enforced" else None
                    ):
                        result = await handler(**kwargs)
                        if isinstance(result, (ChatResponse, StructuredResponse)):
                            if (
                                isinstance(result, ChatResponse)
                                and result.finished_reason == FinishedReason.INTERRUPTED
                            ):
                                if asyncio.current_task().cancelling():
                                    raise asyncio.CancelledError()
                                raise TimeoutError("model interrupted before response")
                            if breaker:
                                await breaker.record_success(
                                    permit, ttft_seconds=time.monotonic() - start
                                )
                            return result
                        stream = result
                        first = await anext(stream)
                    ttft = time.monotonic() - start
                    if first.finished_reason == FinishedReason.INTERRUPTED:
                        if asyncio.current_task().cancelling():
                            raise asyncio.CancelledError()
                        raise TimeoutError("model interrupted before first response")

                    async def output(
                        stream=stream,
                        first=first,
                        breaker=breaker,
                        permit=permit,
                        ttft=ttft,
                    ):
                        try:
                            yield first
                            async for chunk in stream:
                                yield chunk
                            if breaker:
                                await breaker.record_success(permit, ttft_seconds=ttft)
                        except BaseException as error:
                            if breaker:
                                if isinstance(error, Exception) and retryable(error):
                                    await breaker.record_failure(
                                        permit,
                                        failure_kind=CircuitFailureKind.TRANSIENT,
                                    )
                                else:
                                    await breaker.record_inconclusive(permit)
                            raise
                        finally:
                            await stream.aclose()

                    return output()
                except BaseException as error:
                    if stream is not None:
                        await stream.aclose()
                    if breaker and permit:
                        if isinstance(error, Exception) and retryable(error):
                            await breaker.record_failure(
                                permit, failure_kind=CircuitFailureKind.TRANSIENT
                            )
                        else:
                            await breaker.record_inconclusive(permit)
                    if not isinstance(error, Exception):
                        raise
                    if isinstance(error, CircuitOpenError):
                        last_error = error
                        break
                    if not retryable(error):
                        raise
                    if gateway_owned:
                        # Proxy 已执行自己的重试/路由策略；应用不得再次展开候选链。
                        raise
                    last_error = error
                    state["transport_failures"] = state.get("transport_failures", 0) + 1
        raise last_error or RuntimeError("model candidates exhausted")


class ModelPolicyMiddleware(MiddlewareBase):
    def __init__(self, policy, *, state_key="model_route"):
        self.policy, self.state_key = policy, state_key

    async def on_model_call(self, agent, input_kwargs, next_handler):
        state = agent.state.middle_context.setdefault(self.state_key, {})
        return await self.policy.invoke(next_handler, input_kwargs, state)


def merge_fragment(previous, incoming):
    for size in range(min(len(previous), len(incoming)), 0, -1):
        if previous.endswith(incoming[:size]):
            return previous + incoming[size:]
    return previous + incoming


async def recover_output(
    call,
    messages,
    *,
    requested_tokens,
    maximum_tokens,
    continuations=2,
    context_attempts=1,
    compact=None,
    escalation=True,
    state=None,
):
    """有界文本恢复；仅返回完整响应，结构化/工具截断禁止拼接执行。

    流式调用应在公开发送 token 前使用本完整响应策略；已公开的增量不能重放。
    compact 由领域层提供，必须保留来源/覆盖信息，本层不猜测删除证据。
    """
    state = {} if state is None else state
    state.setdefault("continuations", 0)
    state.setdefault("context_attempts", 0)
    state.setdefault("escalated", False)
    state.setdefault("text", "")
    state.setdefault("finish_reasons", [])
    if (
        requested_tokens < 1
        or maximum_tokens < 1
        or continuations < 0
        or context_attempts < 0
    ):
        raise ValueError("invalid output recovery limits")
    if state.get("completed"):
        raise ValueError("completed recovery state cannot execute again")
    if state.get("exhausted"):
        raise RecoveryExhausted("recovery state is exhausted")
    if (
        state["continuations"] > continuations
        or state["context_attempts"] > context_attempts
    ):
        raise RecoveryExhausted("restored recovery budget exceeds configured limits")
    from open_deep_research.agentscope_runtime.messages import dump_messages, load_messages

    original = (
        load_messages(state["compacted_context"])
        if "compacted_context" in state
        else deepcopy(messages)
    )
    current = deepcopy(original)
    limit = (
        maximum_tokens if state["escalated"] else min(requested_tokens, maximum_tokens)
    )
    if state["text"]:
        current += [
            Msg(
                name="assistant",
                role="assistant",
                content=[TextBlock(text=state["text"])],
            ),
            UserMsg("user", "请从中断位置继续，不要重复已有内容。"),
        ]
    while True:
        try:
            response = await call(current, limit)
        except Exception as error:
            code = getattr(error, "code", None) or getattr(
                error, "provider_error_code", None
            )
            if not (
                gateway_error_indicates_token_limit(str(code), str(error))
                and compact
                and state["context_attempts"] < context_attempts
            ):
                raise
            state["context_attempts"] += 1
            original = await compact(deepcopy(original))
            from open_deep_research.agentscope_runtime.messages import validate_tool_pairs

            validate_tool_pairs(original, complete=False)
            state["compacted_context"] = dump_messages(original)
            current = deepcopy(original)
            if state["text"]:
                current += [
                    Msg(
                        name="assistant",
                        role="assistant",
                        content=[TextBlock(text=state["text"])],
                    ),
                    UserMsg("user", "请从中断位置继续，不要重复已有内容。"),
                ]
            continue
        if not isinstance(response, ChatResponse) or not response.is_last:
            raise ValueError("output recovery requires a complete ChatResponse")
        reason = response.metadata.get("provider_finish_reason")
        if reason is None:
            raise RecoveryExhausted(
                "provider finish reason unavailable; cannot certify complete output"
            )
        state["finish_reasons"].append(reason)
        state.setdefault("attempt_usage", []).append(
            {
                "response_id": response.id,
                "usage": asdict(response.usage) if response.usage is not None else None,
            }
        )
        if response.finished_reason == FinishedReason.INTERRUPTED:
            raise RecoveryExhausted("model output interrupted")
        normalized_reason = str(reason).lower()
        truncated = normalized_reason in {
            "length",
            "max_tokens",
            "max_output_tokens",
            "model_length",
        }
        if not truncated and normalized_reason not in {
            "stop",
            "end_turn",
            "stop_sequence",
            "tool_calls",
            "tool_use",
        }:
            raise RecoveryExhausted("provider did not complete output successfully")
        if (
            truncated
            and escalation
            and not state["escalated"]
            and maximum_tokens > requested_tokens
            and all(isinstance(b, (TextBlock, ThinkingBlock)) for b in response.content)
        ):
            # 思考阶段也可能耗尽输出预算。重发原始输入不拼接思考片段，
            # 可安全执行一次上限升级；工具/媒体截断仍禁止此路径。
            state["escalated"] = True
            state["text"] = ""
            limit = maximum_tokens
            current = deepcopy(original)
            continue
        if any(not isinstance(b, TextBlock) for b in response.content) and (
            truncated or state["text"]
        ):
            raise RecoveryExhausted("non-text output cannot be continued")
        text = "".join(b.text for b in response.content if isinstance(b, TextBlock))
        if not truncated:
            if state["text"]:
                response.content = [TextBlock(text=merge_fragment(state["text"], text))]
            response.metadata["recovery_finish_reasons"] = list(state["finish_reasons"])
            state["text"] = ""
            state["completed"] = True
            return response
        state["text"] = merge_fragment(state["text"], text)
        if state["continuations"] >= continuations:
            state["exhausted"] = True
            raise RecoveryExhausted("output continuation budget exhausted")
        state["continuations"] += 1
        current = deepcopy(original) + [
            Msg(
                name="assistant",
                role="assistant",
                content=[TextBlock(text=state["text"])],
            ),
            UserMsg("user", "请从中断位置继续，不要重复已有内容。"),
        ]
