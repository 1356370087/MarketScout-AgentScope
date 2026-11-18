"""Finite business completion through the native reasoning middleware hook."""

import json
from dataclasses import asdict, replace
from uuid import uuid4

from agentscope.message import AssistantMsg, Msg, ToolCallBlock
from agentscope.middleware import MiddlewareBase

from open_deep_research.completion import (
    CompletionDecision,
    CompletionPolicyResult,
    ResearchCompletionPolicy,
)


class BusinessCompletion(MiddlewareBase):
    """Use frozen quality-aware completion, retaining historical delivery semantics."""

    def __init__(self, limit, facts, *, min_sources=1, accept_covered_on_exit=False, enforce_quality=False):
        self.limit, self.facts = limit, facts
        self.reasoning_calls = 0
        self.finished = False
        self.policy = ResearchCompletionPolicy(min_sources=min_sources)
        self.result = None
        self.accept_covered_on_exit = accept_covered_on_exit
        self.before_reasoning = None
        self.enforce_quality = enforce_quality

    def evaluate(self, *, explicit=False, exhausted=False):
        context = self.facts()
        if self.enforce_quality:
            context = replace(context, explicit_completion_succeeded=explicit,
                has_remaining_budget=context.has_remaining_budget and not exhausted,
                exhausted_reason=context.exhausted_reason or ("iteration_limit" if exhausted else None))
            self.result = self.policy.evaluate(context)
            return self.result
        # Historical v22-and-earlier runs retain delivery-driven closing.
        if context.cancelled:
            self.result = CompletionPolicyResult(CompletionDecision.TERMINATE, "cancelled")
        elif context.active_task_count:
            self.result = CompletionPolicyResult(
                CompletionDecision.CONTINUE_WITH_GAPS, "active_tasks", ("active_tasks",)
            )
        elif context.research_stop_reason or not context.has_remaining_budget:
            self.result = CompletionPolicyResult(
                CompletionDecision.COMPLETE_PARTIAL,
                context.research_stop_reason or context.exhausted_reason or "budget_exhausted",
            )
        elif explicit:
            self.result = CompletionPolicyResult(
                CompletionDecision.COMPLETE if context.evidence_count else CompletionDecision.COMPLETE_PARTIAL,
                "explicit_completion" if context.evidence_count else "insufficient_evidence",
            )
        elif exhausted:
            self.result = CompletionPolicyResult(CompletionDecision.COMPLETE_PARTIAL, "iteration_limit")
        else:
            self.result = CompletionPolicyResult(
                CompletionDecision.CONTINUE_WITH_GAPS, "explicit_completion_required", ("explicit_completion",)
            )
        return self.result

    def request_completion(self):
        result = self.evaluate(explicit=True)
        if result.action is CompletionDecision.TERMINATE:
            raise ValueError("research terminated: " + result.reason)
        if result.action is CompletionDecision.CONTINUE_WITH_GAPS:
            raise ValueError("research completion pending: " + "; ".join(result.gaps))
        self.finished = True

    async def on_reasoning(self, agent, input_kwargs, next_handler):
        if self.before_reasoning:
            await self.before_reasoning(agent)
        exhausted = self.reasoning_calls >= self.limit
        result = self.evaluate(explicit=self.finished, exhausted=exhausted)
        if (
            self.finished
            or result.action is CompletionDecision.TERMINATE
            or exhausted
            or not self.facts().has_remaining_budget
        ):
            agent.state.middle_context["business_completion"] = asdict(result)
            yield AssistantMsg(agent.name, result.reason)
            return
        self.reasoning_calls += 1
        async for item in next_handler(**input_kwargs):
            if isinstance(item, Msg) and not any(
                isinstance(block, ToolCallBlock) for block in item.content
            ):
                result = self.evaluate(exhausted=self.reasoning_calls >= self.limit)
                if result.action is CompletionDecision.CONTINUE_WITH_GAPS:
                    item = ToolCallBlock(
                        id=uuid4().hex,
                        name="think_tool",
                        input=json.dumps(
                            {
                                "reflection": (
                                    "等待已启动的研究任务结束并收集结果，再调用 ResearchComplete。"
                                    if "active_tasks" in result.gaps else
                                    ("根据验收结果补齐具体需求缺口；仅在预算和补证轮次内继续研究。"
                                     if self.enforce_quality else
                                     "收集已交付结果后调用 ResearchComplete；交接与收尾质量评审已跳过，"
                                     "不要因覆盖台账未评估而重复派发任务。")
                                )
                            },
                            ensure_ascii=False,
                        ),
                    )
                    agent.state.append_context(agent.name, [item])
            yield item
        if self.result:
            agent.state.middle_context["business_completion"] = asdict(self.result)
