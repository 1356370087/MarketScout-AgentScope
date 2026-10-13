"""Research embeddings share the Gateway's fenced model receipts and budgets."""

import asyncio
import json
from contextlib import suppress
from uuid import NAMESPACE_URL, uuid5

import httpx
from openai import APIStatusError
from openai.types import CreateEmbeddingResponse

from open_deep_research.agentscope_runtime.recovery_store import UnknownOperation, digest
from open_deep_research.budgets import BudgetDimension, BudgetExhausted
from open_deep_research.sandbox.internal_api import (
    BudgetReserveRequest,
    OperationGetRequest,
    OperationTransitionRequest,
)


class GatewayEmbeddingAccounting:
    def __init__(self, runtime, request, context):
        self.runtime, self.request, self.context = runtime, request, context

    async def invoke(self, model, texts, call):
        request = self.request
        payload = {"model": model, "input": texts}
        fingerprint = digest(payload)
        operation_id = uuid5(NAMESPACE_URL,
            f"{request.run_id}:{request.logical_operation_id}:embedding:{fingerprint}").hex
        lock = self.runtime.operation_locks.setdefault((request.run_id, operation_id), asyncio.Lock())
        async with lock:
            return await self._invoke(operation_id, fingerprint, payload, call)

    async def _invoke(self, operation_id, fingerprint, payload, call):
        request, internal = self.request, self.runtime.internal
        identity = {"run_id": request.run_id, "fence_token": self.context.fence_token,
                    "logical_operation_id": operation_id}
        existing = await internal.post("/internal/sandbox/operations/get",
            internal.signed(OperationGetRequest, **identity, request_digest=fingerprint))
        if existing.get("found"):
            record = existing["operation"]
            response = (record.get("outcome") or {}).get("embedding_response")
            if record.get("status") == "completed" and response is not None:
                # Match the SDK's parsing of optional/missing provider usage.
                return CreateEmbeddingResponse.construct(**response)
            # A lost reply or a failed prior attempt never authorizes another charge.
            raise UnknownOperation("embedding_operation_not_completed")

        reserve = internal.signed(BudgetReserveRequest, **identity,
            task_id=request.task_id, stage=request.stage, agent_role="embedding",
            physical_attempt_id=operation_id, model_name=payload["model"],
            estimated_input_tokens=len(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
            estimated_output_tokens=0, request_digest=fingerprint,
            trace_metadata={"purpose": "document_embedding", "parent_tool_call_id": request.tool_call_id,
                            "logical_call_id": operation_id})
        try:
            await internal.post("/internal/sandbox/budgets/reserve", reserve)
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 429:
                detail = error.response.json().get("detail", "")
                if detail.startswith("budget_exhausted:"):
                    raise BudgetExhausted(BudgetDimension(detail.split(":", 1)[1])) from None
            raise

        async def commit(status, outcome):
            await internal.post("/internal/sandbox/operations/transition",
                internal.signed(OperationTransitionRequest, **identity, status=status,
                                outcome=outcome, error_type=outcome.get("error_code")))

        tags = [f"run:{request.run_id}", f"operation:{operation_id}",
                "role:embedding", f"stage:{request.stage}"]
        try:
            response = await call(extra_body={"metadata": {"tags": tags}},
                                  extra_headers={"x-litellm-request-id": operation_id})
        except BaseException as error:
            code = {400: "invalid_request", 401: "authentication", 403: "permission_denied"}.get(
                error.status_code if isinstance(error, APIStatusError) else None)
            with suppress(Exception):
                await commit("failed" if code else "uncertain", {
                    "requested_model": payload["model"], "usage": {},
                    "error_code": code or "embedding_outcome_unknown"})
            raise
        usage = response.usage
        await commit("completed", {
            "requested_model": payload["model"], "served_model": response.model,
            "usage": {"input_tokens": usage.prompt_tokens, "output_tokens": 0} if usage else {},
            "embedding_response": response.model_dump(mode="json"),
        })
        return response
