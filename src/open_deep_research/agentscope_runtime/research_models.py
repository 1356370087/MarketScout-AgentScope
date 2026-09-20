"""Native model boundary shared by research stages and worker agents."""

from agentscope.message import TextBlock, UserMsg

from open_deep_research.agentscope_runtime.context import NativeContextCompactor


class ResearchModels:
    """Keep M3 credential, retry and output-recovery ownership in ModelFactory."""

    def __init__(
        self, factory, *, model_for=None, context_chars=120_000, recovery=None
    ):
        self.factory = factory
        self.recovery = recovery
        self.model_for = model_for
        self.context_chars = context_chars

    def agent_model(self, role, task_id):
        return (
            self.model_for(role, task_id)
            if self.model_for
            else self.factory.build(role)
        )

    def pricing(self, role):
        descriptor = self.factory.descriptor(role)
        entry = self.factory.run.get("model_catalog_snapshot").get(descriptor["model"])
        if entry:
            return entry["input_cost_per_token"] * 1_000_000, entry[
                "output_cost_per_token"
            ] * 1_000_000
        return None

    def agent_middlewares(self, role, model):
        policy = self.factory.policy_middleware(
            role, candidates=[model] if self.model_for else None
        )
        if not self.recovery:
            return [policy]
        from open_deep_research.agentscope_runtime.recovery import JournalMiddleware
        from open_deep_research.agentscope_runtime.telemetry import NativeTelemetryMiddleware

        maximum = self.factory.descriptor(role)["max_output_tokens"]
        return [
            NativeTelemetryMiddleware(self.recovery, role),
            JournalMiddleware(
                self.recovery, role, maximum, self.pricing(role),
                account_attempts=getattr(self.factory, "accounts_physical_attempts", False),
            ),
            policy,
        ]

    @staticmethod
    def _attach_attempt_summary(response, policy_state):
        """把本次逻辑调用的物理尝试记账附到响应元数据，供 SQL 结算读取。"""
        if policy_state.get("physical_attempts") is None:
            return
        metadata = getattr(response, "metadata", None)
        if isinstance(metadata, dict):
            metadata["model_attempts"] = {
                "physical_attempts": policy_state.get("physical_attempts") or 0,
                "attempt_failures": policy_state.get("attempt_failures") or [],
            }

    async def context_summary(self, role, model, messages, schema, *, operation_key=None):
        """Account and replay the SDK's direct structured summary model call."""
        import json
        import jsonschema
        from agentscope.exception import StructuredOutputError
        from pydantic import ValidationError
        from open_deep_research.agentscope_runtime.gateway import GatewayCallError
        from open_deep_research.agentscope_runtime.model_policy import retryable
        from open_deep_research.agentscope_runtime.model_accounting import RecordedModelFailure
        from open_deep_research.agentscope_runtime.native_context import ContextSummaryFailed
        from open_deep_research.models.errors import is_token_limit_exceeded

        policy = self.factory.policy_middleware(role, candidates=[model] if self.model_for else None)
        route = {}

        async def invoke(current_model, messages, **kwargs):
            return await current_model.generate_structured_output(messages, schema)

        async def call():
            try:
                response = await policy.policy.invoke(invoke, {"messages": messages}, route)
                self._attach_attempt_summary(response, route)
                return response
            except Exception as error:
                # Control, authentication, uncertain execution and storage errors
                # are not permission to drop research history.
                known_output = isinstance(error, (
                    jsonschema.ValidationError, ValidationError, json.JSONDecodeError, StructuredOutputError,
                )) or (isinstance(error, RecordedModelFailure) and str(error) in {
                    "ValidationError", "JSONDecodeError", "StructuredOutputError",
                }) or (isinstance(error, GatewayCallError) and not error.uncertain and str(error) in {
                    "structured_output_missing", "structured_output_truncated",
                })
                if getattr(error, "uncertain", False) or not (
                    known_output or retryable(error)
                    or is_token_limit_exceeded(error, self.factory.descriptor(role)["model"])
                ):
                    raise
                raise ContextSummaryFailed(type(error).__name__, {
                    "physical_attempts": route.get("physical_attempts", 0),
                    "attempt_failures": route.get("attempt_failures", []),
                }) from error

        if not self.recovery:
            return await call()
        return await self.recovery.model(
            role, messages, call, schema=schema, operation_key=operation_key,
            request_details={"purpose": "context_summary"},
            max_tokens=self.factory.descriptor(role)["max_output_tokens"],
            pricing=self.pricing(role),
            account_attempts=getattr(self.factory, "accounts_physical_attempts", False),
        )

    async def structured(self, role, prompt, schema, state, *, messages=None):
        candidates = [self.model_for(role, "pipeline")] if self.model_for else None
        middleware = self.factory.policy_middleware(role, candidates=candidates)
        messages = messages if messages is not None else [UserMsg("user", prompt)]

        async def invoke(current_model, messages, **kwargs):
            return await current_model.generate_structured_output(messages, schema)

        async def call():
            response = await middleware.policy.invoke(
                invoke, {"messages": messages}, state
            )
            self._attach_attempt_summary(response, state)
            return response

        response = (
            await self.recovery.model(
                role,
                messages,
                call,
                schema=schema,
                max_tokens=self.factory.descriptor(role)["max_output_tokens"],
                pricing=self.pricing(role),
                account_attempts=getattr(self.factory, "accounts_physical_attempts", False),
            )
            if self.recovery
            else await call()
        )
        return schema.model_validate(response.content)

    async def text(self, role, prompt, state):
        async def call():
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
            self._attach_attempt_summary(response, state.get("route", {}))
            return response

        response = (
            await self.recovery.model(
                role,
                [UserMsg("user", prompt)],
                call,
                max_tokens=self.factory.descriptor(role)["max_output_tokens"],
                pricing=self.pricing(role),
                account_attempts=getattr(self.factory, "accounts_physical_attempts", False),
            )
            if self.recovery
            else await call()
        )
        content = "".join(
            block.text for block in response.content if isinstance(block, TextBlock)
        ).strip()
        if not content:
            raise ValueError("research model returned empty text")
        return content
