"""Finite business completion through the native reasoning middleware hook."""

import json
from dataclasses import asdict, replace
from uuid import uuid4

from agentscope.message import AssistantMsg, Msg, ToolCallBlock
from agentscope.middleware import MiddlewareBase

from open_deep_research.completion import CompletionDecision, ResearchCompletionPolicy


class BusinessCompletion(MiddlewareBase):
    """Ask native ReAct for bounded remediation; never implement another loop."""

    def __init__(self, limit, facts, *, min_sources=1):
        self.limit, self.facts = limit, facts
        self.reasoning_calls = 0
        self.finished = False
        self.policy = ResearchCompletionPolicy(min_sources=min_sources)
        self.result = None

    def evaluate(self, *, explicit=False, exhausted=False):
        context = self.facts()
        context = replace(
            context,
            explicit_completion_succeeded=explicit,
            has_remaining_budget=context.has_remaining_budget and not exhausted,
            exhausted_reason=context.exhausted_reason
            or ("iteration_limit" if exhausted else None),
        )
        self.result = self.policy.evaluate(context)
        return self.result

    def request_completion(self):
        result = self.evaluate(explicit=True)
        if result.action is CompletionDecision.CONTINUE_WITH_GAPS:
            raise ValueError("research gaps: " + "; ".join(result.gaps))
        self.finished = True

    async def on_reasoning(self, agent, input_kwargs, next_handler):
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
                                "reflection": "继续补研，解决明确缺口："
                                + "; ".join(result.gaps)
                            },
                            ensure_ascii=False,
                        ),
                    )
                    agent.state.append_context(agent.name, [item])
            yield item
        if self.result:
            agent.state.middle_context["business_completion"] = asdict(self.result)
