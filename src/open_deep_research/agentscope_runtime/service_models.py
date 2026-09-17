"""Native service-model calls for knowledge operations outside research runs."""

from agentscope.credential import OpenAICredential
from agentscope.message import SystemMsg, TextBlock, UserMsg
from agentscope.model import OpenAIChatModel
from pydantic import SecretStr


async def service_text(*, model, api_key, base_url, system, prompt, timeout=120):
    """Use the configured service credential; the gateway owns transport retries."""
    native = OpenAIChatModel(
        model=model,
        credential=OpenAICredential(api_key=SecretStr(api_key), base_url=base_url),
        stream=False,
        max_retries=0,
        client_kwargs={"timeout": timeout, "max_retries": 0},
    )
    try:
        response = await native(
            messages=[SystemMsg("system", system), UserMsg("user", prompt)]
        )
        return "".join(
            block.text for block in response.content if isinstance(block, TextBlock)
        )
    finally:
        await native.client.close()
