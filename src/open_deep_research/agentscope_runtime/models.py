"""原生模型角色与作用域凭据绑定（T019）；网关传输策略由 T020 接入。"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit
from pydantic import SecretStr
from agentscope import credential as credentials
from agentscope.agent import ModelConfig
from open_deep_research.agentscope_runtime.run_config import RunConfig
from open_deep_research.models.resolution import (
    build_model_config,
    parse_model_spec,
    resolve_compatibility_kwargs,
)

# 角色 -> 模型字段、空值回退字段、输出上限字段。
ROLES = {
    "memory": ("research_model", "research_model", "research_model_max_tokens"),
    "supervisor": ("supervisor_model", "research_model", "research_model_max_tokens"),
    "researcher": ("research_model", "research_model", "research_model_max_tokens"),
    "summarization": (
        "summarization_model",
        "summarization_model",
        "summarization_model_max_tokens",
    ),
    "message_summary": (
        "message_summary_model",
        "summarization_model",
        "message_summary_model_max_tokens",
    ),
    "compression": (
        "compression_model",
        "compression_model",
        "compression_model_max_tokens",
    ),
    "final_report": (
        "final_report_model",
        "final_report_model",
        "final_report_model_max_tokens",
    ),
    "quality_evaluation": (
        "quality_evaluation_model",
        "research_model",
        "quality_evaluation_model_max_tokens",
    ),
    "report_review": (
        "report_review_model",
        "quality_evaluation_model",
        "report_review_model_max_tokens",
    ),
    "report_revisor": (
        "final_report_model",
        "final_report_model",
        "final_report_model_max_tokens",
    ),
    "web_rerank": (
        "web_rerank_model",
        "summarization_model",
        "summarization_model_max_tokens",
    ),
    "web_evidence": (
        "web_evidence_model",
        "summarization_model",
        "summarization_model_max_tokens",
    ),
    "egress_classifier": (
        "egress_classifier_model",
        "quality_evaluation_model",
        "quality_evaluation_model_max_tokens",
    ),
}


@dataclass(frozen=True, slots=True)
class CredentialBinding:
    reference: str
    scope: Literal["run", "service"]
    owner: str
    allowed_models: tuple[str, ...]
    key: SecretStr = field(repr=False)
    base_url: str | None = None
    gateway: bool = False

    def __post_init__(self):
        if self.base_url:
            parsed = urlsplit(self.base_url)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError(
                    "credential endpoint must not contain secrets or query parameters"
                )


def bind_role(
    run: RunConfig,
    role: str,
    *,
    reference: str,
    scope: Literal["run", "service"],
    owner: str,
    source: dict | None = None,
) -> CredentialBinding:
    """直接提供商绑定沿用角色 key > provider key 及 GET_API_KEYS_FROM_CONFIG。"""
    model_field, fallback, tokens_field = ROLES[role]
    model = run.get(model_field) or run.get(fallback)
    options = build_model_config(
        model,
        run.get(tokens_field),
        source,
        role=role,
        tags=False,
        configured_base_url=run.get("quality_evaluation_base_url")
        if role == "quality_evaluation"
        else None,
    )
    if not options.get("api_key"):
        raise ValueError(f"missing credential for role: {role}")
    return CredentialBinding(
        reference,
        scope,
        owner,
        (model,),
        SecretStr(options["api_key"]),
        options.get("base_url"),
    )


class ModelFactory:
    """只在内存持有凭据和 SDK 实例；调用者负责运行/服务作用域的权威身份。"""

    accounts_physical_attempts = True

    def __init__(
        self,
        run: RunConfig,
        *,
        scope: Literal["run", "service"],
        owner: str,
        bindings: dict[str, CredentialBinding],
    ):
        self.run, self.scope, self.owner = run, scope, owner
        self._bindings = dict(bindings)
        self._models = {}
        self._policies = {}
        self._closed = False

    def descriptor(self, role: str, candidate_spec: str | None = None) -> dict:
        if role not in ROLES:
            raise ValueError("unsupported model role")
        model_field, fallback, tokens = ROLES[role]
        spec = self.run.get(model_field) or self.run.get(fallback)
        if candidate_spec is not None:
            allowed = [spec, *self.run.get("model_fallbacks").get(role, [])]
            if candidate_spec not in allowed:
                raise ValueError("candidate is not in frozen role chain")
            spec = candidate_spec
        binding = self._bindings[role]
        if binding.scope != self.scope or binding.owner != self.owner:
            raise ValueError("credential scope mismatch")
        if spec not in binding.allowed_models:
            raise ValueError("model is not authorized by credential binding")
        catalog = self.run.get("model_catalog_snapshot")
        if binding.gateway and spec not in catalog:
            raise ValueError("gateway model is missing from frozen catalog")
        return {
            "role": role,
            "model": spec,
            "max_output_tokens": self.run.get(tokens),
            "credential_reference": binding.reference,
            "scope": binding.scope,
        }

    def build(self, role: str, candidate_spec: str | None = None):
        if self._closed:
            raise RuntimeError("model factory is closed")
        descriptor = self.descriptor(role, candidate_spec)
        if self.run.get("sandbox_enabled"):
            raise ValueError(
                "sandbox requires T020 gateway adapter; direct model forbidden"
            )
        binding = self._bindings[role]
        if self.run.get("model_backend") == "litellm" and not binding.gateway:
            raise ValueError("LiteLLM requires an authorized gateway binding")
        if binding.gateway and not binding.base_url:
            raise ValueError("gateway binding requires a Proxy endpoint")
        cache_key = (role, descriptor["model"])
        if cache_key in self._models:
            return self._models[cache_key]
        provider, model_name = parse_model_spec(descriptor["model"])
        if binding.gateway:
            provider, model_name = "openai", descriptor["model"]
        classes = {
            "openai": credentials.OpenAICredential,
            "anthropic": credentials.AnthropicCredential,
            "deepseek": credentials.DeepSeekCredential,
            "google": credentials.GeminiCredential,
            "google_genai": credentials.GeminiCredential,
        }
        if provider not in classes:
            raise ValueError("provider requires an explicit native adapter or gateway")
        credential_class = classes[provider]
        args = {"id": binding.reference, "api_key": binding.key}
        if binding.base_url:
            if "base_url" not in credential_class.model_fields:
                raise ValueError("provider credential does not support base_url")
            args["base_url"] = binding.base_url
        credential = credential_class(**args)
        model_class = credential.get_chat_model_class()
        if provider == "openai":
            from open_deep_research.agentscope_runtime.gateway import (
                GovernedOpenAIChatModel,
                LiteLLMChatModel,
            )

            model_class = (
                LiteLLMChatModel if binding.gateway else GovernedOpenAIChatModel
            )
        else:
            from open_deep_research.agentscope_runtime.provider_models import (
                GovernedAnthropicChatModel,
                GovernedGeminiChatModel,
                GovernedDeepSeekChatModel,
            )

            model_class = {
                "anthropic": GovernedAnthropicChatModel,
                "deepseek": GovernedDeepSeekChatModel,
                "google": GovernedGeminiChatModel,
                "google_genai": GovernedGeminiChatModel,
            }[provider]
        fields = model_class.Parameters.model_fields
        token_field = "max_tokens" if "max_tokens" in fields else "max_output_tokens"
        catalog = self.run.get("model_catalog_snapshot")
        cap = catalog.get(descriptor["model"], {}).get(
            "max_output_tokens", descriptor["max_output_tokens"]
        )
        parameter_values = {token_field: min(cap, descriptor["max_output_tokens"])}
        if role == "quality_evaluation":
            parameter_values["temperature"] = self.run.get(
                "quality_evaluation_temperature"
            )
        elif role in {"report_review", "report_revisor"}:
            parameter_values["temperature"] = self.run.get("report_review_temperature")
        parameters = model_class.Parameters(**parameter_values)
        kwargs = {
            "credential": credential,
            "model": model_name,
            "parameters": parameters,
            "max_retries": 0,
            "client_kwargs": {"max_retries": 0},
        }
        if descriptor["model"] in catalog:
            kwargs["context_size"] = catalog[descriptor["model"]]["context_window"]
        if provider == "openai" and not binding.gateway:
            kwargs.update(
                resolve_compatibility_kwargs(descriptor["model"], binding.base_url)
            )
        # Gemini 的 SDK 没有 OpenAI/Anthropic client 的 max_retries 参数。
        if provider in {"google", "google_genai"}:
            kwargs["client_kwargs"] = {
                "http_options": {"retry_options": {"attempts": 1}}
            }
        instance = model_class(**kwargs)
        instance.retry_owner = "gateway" if binding.gateway else "application"
        entry = catalog.get(descriptor["model"])
        instance.accounting_price = (
            (entry["input_cost_per_token"] * 1_000_000, entry["output_cost_per_token"] * 1_000_000)
            if entry else None
        )
        self._models[cache_key] = instance
        return instance

    @staticmethod
    def agent_model_config() -> ModelConfig:
        return ModelConfig(max_retries=0)

    def build_sandbox(self, role, binding, *, client=None):
        from open_deep_research.agentscope_runtime.gateway import SandboxChatModel

        if (
            self._closed
            or binding.role != role
            or self.scope != "run"
            or binding.run_id != self.owner
        ):
            raise ValueError("sandbox model binding scope mismatch")
        field, fallback, tokens = ROLES[role]
        key = f"sandbox:{role}:{binding.task_id}"
        if key not in self._models:
            self._models[key] = SandboxChatModel(
                binding=binding,
                model=self.run.get(field) or self.run.get(fallback),
                parameters=SandboxChatModel.Parameters(max_tokens=self.run.get(tokens)),
                client=client,
                structured_attempts=self.run.get("max_structured_output_retries"),
            )
        return self._models[key]

    def policy_middleware(self, role, candidates=None):
        from open_deep_research.agentscope_runtime.model_policy import (
            ModelCallPolicy,
            ModelPolicyMiddleware,
        )
        from open_deep_research.models.circuit import ModelCircuitPolicy

        if candidates is None and role in self._policies:
            return self._policies[role]
        cache_policy = candidates is None

        if candidates is None:
            specs = [
                self.descriptor(role)["model"],
                *self.run.get("model_fallbacks").get(role, []),
            ]
            candidates = [self.build(role, spec) for spec in dict.fromkeys(specs)]
        policy = ModelCircuitPolicy(
            failure_threshold=self.run.get("model_circuit_failure_threshold"),
            open_cooldown_seconds=self.run.get("model_circuit_open_cooldown_seconds"),
            failure_window_seconds=self.run.get("model_circuit_failure_window_seconds"),
            max_cooldown_seconds=self.run.get("model_circuit_max_cooldown_seconds"),
            slow_ratio_threshold=self.run.get("model_circuit_slow_ratio_threshold"),
            slow_min_samples=self.run.get("model_circuit_slow_min_samples"),
            first_packet_probe=self.run.get("model_first_packet_probe"),
            slow_first_packet_threshold_seconds=self.run.get(
                "model_slow_first_packet_threshold_seconds"
            ),
        )
        middleware = ModelPolicyMiddleware(
            ModelCallPolicy(
                candidates,
                attempts=self.run.get("model_transport_max_attempts"),
                first_packet_timeout=self.run.get("model_first_packet_timeout_seconds"),
                circuit_policy=policy,
                circuit_enabled=self.run.get("model_circuit_breaker_enabled"),
                probe_mode=self.run.get("model_first_packet_probe"),
            ),
            state_key=f"model_route:{role}",
        )
        if cache_policy:
            self._policies[role] = middleware
        return middleware

    async def complete_with_recovery(self, role, messages, *, state, compact=None, candidates=None):
        """写作/摘要完整响应路径；模型尝试仍通过统一策略，恢复状态可写入 AgentState。"""
        from open_deep_research.agentscope_runtime.model_policy import recover_output
        from agentscope.model import ChatResponse

        middleware = self.policy_middleware(role, candidates=candidates)
        descriptor = self.descriptor(role)

        async def call(current, limit):
            async def handler(current_model, messages, **kwargs):
                return await current_model(messages=messages, max_tokens=limit)

            result = await middleware.policy.invoke(
                handler, {"messages": current, "accounting_max_tokens": limit}, state.setdefault("route", {})
            )
            if isinstance(result, ChatResponse):
                return result
            final = None
            try:
                async for chunk in result:
                    if chunk.is_last:
                        final = chunk
            finally:
                await result.aclose()
            if final is None:
                raise RuntimeError("model stream ended without final response")
            return final

        spec = descriptor["model"]
        catalog = self.run.get("model_catalog_snapshot")
        maximum = self.run.get("model_max_output_tokens_overrides").get(
            spec,
            catalog.get(spec, {}).get(
                "max_output_tokens", descriptor["max_output_tokens"]
            ),
        )
        if spec in catalog:
            maximum = min(maximum, catalog[spec]["max_output_tokens"])
        return await recover_output(
            call,
            messages,
            requested_tokens=descriptor["max_output_tokens"],
            maximum_tokens=maximum,
            continuations=self.run.get("output_continuation_max_attempts"),
            context_attempts=self.run.get("context_recovery_max_attempts"),
            escalation=self.run.get("output_token_escalation_enabled"),
            compact=compact,
            state=state.setdefault("output", {}),
        )

    async def aclose(self):
        self._closed = True
        self._policies.clear()
        models, self._models = self._models, {}
        for model in models.values():
            from open_deep_research.agentscope_runtime.gateway import SandboxChatModel

            if isinstance(model, SandboxChatModel):
                await model.aclose()
                continue
            client = getattr(model, "client", None)
            if client is not None:
                from agentscope.model import GeminiChatModel

                if isinstance(model, GeminiChatModel):
                    # google-genai 的同步 close 不关闭 aio 客户端。
                    try:
                        await client.aio.aclose()
                    finally:
                        client.close()
                    continue
                close = getattr(client, "close", None) or getattr(
                    client, "aclose", None
                )
                if close is not None:
                    import inspect

                    result = close()
                    if inspect.isawaitable(result):
                        await result
