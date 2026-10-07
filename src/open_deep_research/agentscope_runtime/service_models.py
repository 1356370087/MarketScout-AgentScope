"""Service-scoped text calls through the native model factory and policy."""

from agentscope.message import SystemMsg, TextBlock, UserMsg
from agentscope.model import ChatResponse
from pydantic import SecretStr

from open_deep_research.configuration import freeze_run_config
from open_deep_research.models.catalog import (
    LiteLLMModelCatalogClient,
    freeze_catalog_snapshot,
    validate_model_catalog,
)

from .models import CredentialBinding, ModelFactory
from .run_config import RunConfig


async def policy_text(models, role, *, system, prompt):
    """Consume the response inside policy accounting, including streaming adapters."""
    messages = [SystemMsg("system", system), UserMsg("user", prompt)]

    async def handler(current_model, messages, **kwargs):
        response = await current_model(messages=messages)
        if isinstance(response, ChatResponse):
            return response
        final = None
        try:
            async for chunk in response:
                if chunk.is_last:
                    final = chunk
        finally:
            await response.aclose()
        if final is None:
            raise RuntimeError("knowledge_model_missing_response")
        return final

    response = await models.policy_middleware(role).policy.invoke(handler, {"messages": messages}, {})
    return "".join(block.text for block in response.content if isinstance(block, TextBlock))


async def service_text(*, model, api_key, base_url, system, prompt, timeout=120, operation="service"):
    """Use a service binding and count each attempt in the current knowledge query."""
    from open_deep_research.knowledge.accounting import (
        ServiceAttemptAccounting,
        query_account,
    )

    from .model_accounting import current_accounting

    role = "summarization"
    catalog_client = LiteLLMModelCatalogClient(base_url=base_url, api_key=api_key)
    try:
        catalog = await catalog_client.load()
    finally:
        await catalog_client.aclose()
    validate_model_catalog(catalog, [model], budget_enabled=False)
    run = RunConfig.compile(freeze_run_config({"configurable": {
        "model_backend": "litellm", "summarization_model": model,
        "model_catalog_snapshot": freeze_catalog_snapshot(catalog, [model]),
        "model_fallbacks": {},
        "model_transport_max_attempts": 1, "model_first_packet_timeout_seconds": timeout,
    }}, prefer_configurable=True))
    owner = query_account.get()[0] if query_account.get() else "knowledge-service"
    binding = CredentialBinding("knowledge-service", "service", owner, (model,),
                                SecretStr(api_key), base_url, gateway=True)
    factory = ModelFactory(run, scope="service", owner=owner, bindings={role: binding})
    token = current_accounting.set(ServiceAttemptAccounting(operation) if query_account.get() else None)
    try:
        factory.build(role).stream = False
        return await policy_text(factory, role, system=system, prompt=prompt)
    finally:
        current_accounting.reset(token)
        await factory.aclose()
