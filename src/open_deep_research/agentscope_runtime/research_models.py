"""Native model boundary shared by research stages and worker agents."""

from agentscope.message import TextBlock, UserMsg

from open_deep_research.agentscope_runtime.context import NativeContextCompactor


class ResearchModels:
    """Keep M3 credential, retry and output-recovery ownership in ModelFactory."""

    def __init__(self, factory, *, model_for=None, context_chars=120_000):
        self.factory = factory
        self.model_for = model_for
        self.context_chars = context_chars

    def agent_model(self, role, task_id):
        return (
            self.model_for(role, task_id)
            if self.model_for
            else self.factory.build(role)
        )

    def agent_middlewares(self, role, model):
        return [
            self.factory.policy_middleware(
                role, candidates=[model] if self.model_for else None
            )
        ]

    async def structured(self, role, prompt, schema, state):
        candidates = [self.model_for(role, "pipeline")] if self.model_for else None
        middleware = self.factory.policy_middleware(role, candidates=candidates)
        messages = [UserMsg("user", prompt)]

        async def invoke(current_model, messages, **kwargs):
            return await current_model.generate_structured_output(messages, schema)

        response = await middleware.policy.invoke(invoke, {"messages": messages}, state)
        return schema.model_validate(response.content)

    async def text(self, role, prompt, state):
        response = await self.factory.complete_with_recovery(
            role,
            [UserMsg("user", prompt)],
            state=state,
            candidates=[self.model_for(role, state.get("task_id", "pipeline"))]
            if self.model_for
            else None,
            compact=NativeContextCompactor(
                max_chars=self.context_chars, protected_ids=set()
            ),
        )
        content = "".join(
            block.text for block in response.content if isinstance(block, TextBlock)
        ).strip()
        if not content:
            raise ValueError("research model returned empty text")
        return content
