"""Secret-free ModelGateway client used inside Sandbox Workers."""

from __future__ import annotations

import os
import secrets
import time
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from open_deep_research.models.codec import (
    decode_message,
    encode_messages,
    normalize_tool_definition,
    parse_structured_output,
    structured_output_tool,
)
from open_deep_research.models.gateway import (
    ModelGatewayError,
    ModelRequest,
    ModelResult,
    ModelRoute,
    ModelUsage,
)
from open_deep_research.sandbox.wire import GatewayModelOutcomeV2, GatewayModelRequestV2

T = TypeVar("T", bound=BaseModel)


class SandboxModelGateway:
    """Delegate model calls with a task capability instead of a model credential."""

    def __init__(
        self,
        *,
        gateway_url: str | None = None,
        task_token: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Bind the task capability and reusable internal HTTP client."""
        self.gateway_url = (gateway_url or os.getenv("SANDBOX_GATEWAY_URL") or "").rstrip("/")
        self.task_token = task_token or os.getenv("SANDBOX_TASK_TOKEN") or ""
        if not self.gateway_url:
            raise RuntimeError("sandbox_gateway_not_configured")
        if not self.task_token:
            raise RuntimeError("sandbox_task_token_unavailable")
        self._client = client
        self._owns_client = client is None

    @staticmethod
    def _wire_request(request: ModelRequest[Any]) -> GatewayModelRequestV2:
        return GatewayModelRequestV2(
            run_id=request.run_id,
            task_id=request.task_id,
            role=str(request.role),
            stage=request.stage,
            logical_operation_id=request.logical_operation_id,
            model=request.model,
            messages=encode_messages(request.messages),
            tools=[normalize_tool_definition(tool) for tool in request.tools],
            tool_choice=request.tool_choice,
            structured_schema=(
                structured_output_tool(request.output_schema, strict=False)["function"][
                    "parameters"
                ]
                if request.output_schema is not None
                else None
            ),
            max_output_tokens=request.max_output_tokens,
            temperature=request.temperature,
            trace_metadata=dict(request.trace_metadata),
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.task_token}",
            "Content-Type": "application/json",
            "X-Sandbox-Timestamp": str(time.time()),
            "X-Sandbox-Nonce": secrets.token_urlsafe(24),
        }

    async def complete(self, request: ModelRequest[T]) -> ModelResult[T]:
        """Execute one idempotent logical request through Sandbox Gateway."""
        wire = self._wire_request(request)
        client = self._client or httpx.AsyncClient(base_url=self.gateway_url, timeout=None)
        if self._client is None:
            self._client = client
        try:
            response = await client.post(
                "/v2/models/complete",
                content=wire.model_dump_json(),
                headers=self._headers(),
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ModelGatewayError(
                "sandbox_gateway_error",
                status_code=exc.response.status_code,
                request_id=exc.response.headers.get("x-request-id"),
            ) from exc
        except httpx.RequestError as exc:
            raise ModelGatewayError("sandbox_gateway_unavailable") from exc
        outcome = GatewayModelOutcomeV2.model_validate(response.json())
        if outcome.status != "completed" or outcome.message is None:
            raise ModelGatewayError(outcome.error_code or "sandbox_gateway_model_failed")
        message = decode_message(outcome.message)
        from langchain_core.messages import AIMessage

        if not isinstance(message, AIMessage):
            raise ModelGatewayError("sandbox_gateway_non_assistant_response")
        structured = (
            parse_structured_output(
                message,
                request.output_schema,
                payload_transform=request.output_payload_transform,
            )
            if request.output_schema is not None
            else None
        )
        return ModelResult(
            message=message,
            structured=structured,
            usage=ModelUsage(
                input_tokens=int(outcome.usage.get("input_tokens", 0)),
                output_tokens=int(outcome.usage.get("output_tokens", 0)),
                total_tokens=int(outcome.usage.get("total_tokens", 0)),
                cached_input_tokens=int(outcome.usage.get("cached_input_tokens", 0)),
                reasoning_tokens=int(outcome.usage.get("reasoning_tokens", 0)),
            ),
            response_cost_usd=outcome.response_cost_usd,
            request_id=outcome.request_id,
            route=ModelRoute(
                requested_model=outcome.requested_model,
                served_model=outcome.served_model,
                provider=outcome.provider,
                deployment_id=outcome.deployment_id,
            ),
            finish_reason=outcome.finish_reason,
            latency_ms=float(outcome.latency_ms or 0),
        )

    async def aclose(self) -> None:
        """Close the reusable Gateway HTTP connection pool."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()


__all__ = ["SandboxModelGateway"]
