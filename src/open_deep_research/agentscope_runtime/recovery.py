"""Native operation journaling and stage recovery without a second Agent loop."""

from __future__ import annotations

import asyncio
from contextlib import aclosing, contextmanager
from contextvars import ContextVar
from dataclasses import asdict, fields, is_dataclass
from uuid import NAMESPACE_URL, uuid5

from agentscope.message import Msg, UserMsg
from agentscope.middleware import MiddlewareBase
from agentscope.model import ChatResponse, ChatUsage, FinishedReason, StructuredResponse
from agentscope.state import AgentState
from pydantic import BaseModel

from open_deep_research.agentscope_runtime.recovery_store import (
    digest,
)
from open_deep_research.budgets import BudgetExhausted
from open_deep_research.tools.base import ToolEffect, ToolResult
from open_deep_research.tools.governance import (
    GovernedToolCallResult,
    ToolError,
    ToolOutcomeMessage,
)


class ApprovalPending(RuntimeError):
    """An exact operation is parked; execution will restart from its journal."""

    def __init__(self, action_id, kind, payload):
        super().__init__("approval required")
        self.action_id, self.kind, self.payload = action_id, kind, payload


def stable_input(value):
    """Ignore envelope timestamps and runtime clock hints, not business inputs."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {
            key: stable_input(item)
            for key, item in value.items()
            if key not in {"created_at", "finished_at", "usage"}
            and not (
                key == "id" and value.get("type") not in {"tool_call", "tool_result"}
            )
        }
    if isinstance(value, (list, tuple)):
        return [
            stable_input(item)
            for item in value
            if not (isinstance(item, dict) and item.get("type") == "hint")
            and getattr(item, "type", None) != "hint"
        ]
    return value


def response_dump(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return {
            field.name: response_dump(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, dict):
        return {key: response_dump(item) for key, item in value.items()}
    if isinstance(value, list):
        return [response_dump(item) for item in value]
    return value


def _usage_details(response, agent):
    """缓存 token 与物理尝试明细：单独记录，不并入六维结算。

    六维账本的算术只认 ``BudgetDimension``；缓存 token、失败尝试的已计费
    usage 与尝试计数作为回执明细留存，满足"分别计入、缺失不当零"。
    """
    details = {}
    usage = getattr(response, "usage", None)
    if usage is not None:
        for field_name in ("cache_input_tokens", "cached_input_tokens"):
            cached = getattr(usage, field_name, None)
            if cached:
                details["cached_input_tokens"] = cached
                break
    metadata = getattr(response, "metadata", None)
    attempts = (
        metadata.get("model_attempts")
        if isinstance(metadata, dict) and metadata.get("model_attempts")
        else None
    )
    if attempts is None and agent is not None:
        route = (getattr(agent.state, "middle_context", None) or {}).get(
            "model_route"
        )
        if isinstance(route, dict) and route.get("physical_attempts") is not None:
            attempts = {
                "physical_attempts": route.get("physical_attempts"),
                "attempt_failures": route.get("attempt_failures") or [],
            }
    if attempts:
        details.update(attempts)
    return details or None


def response_load(value, *, structured=False):
    result = dict(value)
    if result.get("usage"):
        result["usage"] = ChatUsage(**result["usage"])
    result["finished_reason"] = FinishedReason(result["finished_reason"])
    if structured:
        return StructuredResponse(**result)
    result["content"] = Msg.model_validate(
        {"name": "model", "role": "assistant", "content": result["content"]}
    ).content
    return ChatResponse(**result)


class RecoverySession:
    """A leased run; stage replay reuses committed model and tool results."""

    def __init__(
        self, store, lease, snapshot, *, ttl=30, failpoint=None, approval_applier=None,
        model_accounting="local",
    ):
        self.store, self.lease, self.snapshot = store, lease, snapshot
        self.ttl, self.failpoint = ttl, failpoint
        self.stage = ContextVar("recovery_stage", default="outside")
        self.task_id = ContextVar("recovery_task", default="pipeline")
        self.counters = {}
        self.problem = None
        self.current_command = None
        self.current_limits = None
        self.approval_applier = approval_applier
        self.public_publisher = None
        self.grants = dict(snapshot.approval_grants)
        if model_accounting not in {"local", "gateway"}:
            raise ValueError("unknown model accounting authority")
        self.model_accounting = model_accounting

    @classmethod
    async def open(cls, store, run_id, user_id, *, ttl=30, failpoint=None):
        lease = await store.acquire(run_id, user_id, ttl=ttl)
        try:
            state, _ = await store.load(run_id, user_id)
            if state.status == "running" or state.inflight:
                # Replay is mediated by the operation journal, not blind stage replay.
                state.inflight = None
                state.status = "ready"
            return cls(store, lease, state, ttl=ttl, failpoint=failpoint)
        except BaseException:
            await store.release(lease)
            raise

    async def close(self):
        await self.store.release(self.lease)

    @contextmanager
    def scope(self, stage, generation):
        token = self.stage.set(f"{stage}:{generation}")
        self.counters = {}
        self.problem = None
        try:
            yield
        finally:
            self.stage.reset(token)

    @contextmanager
    def task(self, task_id):
        token = self.task_id.set(task_id)
        try:
            yield
        finally:
            self.task_id.reset(token)

    def key(self, kind):
        prefix = f"{self.stage.get()}:{self.task_id.get()}:{kind}"
        ordinal = self.counters.get(prefix, 0)
        self.counters[prefix] = ordinal + 1
        return f"{prefix}:{ordinal}"

    def assignment_id(self, ordinal):
        return uuid5(
            NAMESPACE_URL,
            f"{self.lease.run_id}:{self.stage.get()}:assignment:{ordinal}",
        ).hex

    async def save(self, snapshot):
        await self.store.save(
            self.lease,
            snapshot,
            command_id=self.current_command,
            limits=self.current_limits,
        )
        self.snapshot = snapshot.model_copy(deep=True)
        self.grants = dict(snapshot.approval_grants)
        self.current_command = None
        self.current_limits = None
        if self.public_publisher is not None:
            await self.store.deliver_public(self.lease, self.public_publisher)

    async def hit(self, name):
        if self.failpoint:
            await self.failpoint(name)

    async def operation(
        self,
        kind,
        payload,
        call,
        *,
        key=None,
        replay_safe=False,
        reserve=None,
        actual=None,
    ):
        key = key or self.key(kind)
        if self.problem:
            raise self.problem
        try:
            record = await self.store.begin_operation(
                self.lease,
                key,
                kind,
                stable_input(payload),
                replay_safe=replay_safe,
                reserve=reserve,
            )
            if record["replayed"]:
                return record["result"]
            await self.hit("operation_planned")
            result = await call()
            await self.hit("effect_returned")
            await self.store.commit_operation(
                self.lease, key, result, actual=actual(result) if actual else None
            )
            await self.hit("operation_committed")
            return result
        except BudgetExhausted as exc:
            action = digest([self.lease.run_id, key, "budget"])
            self.problem = ApprovalPending(
                action,
                "budget",
                {"operation_key": key, "dimension": exc.dimension.value},
            )
            raise self.problem from exc
        except BaseException as exc:
            self.problem = exc
            raise

    async def model(
        self,
        role,
        messages,
        call,
        *,
        schema=None,
        max_tokens=4096,
        pricing=None,
        request_details=None,
        agent=None,
        account_attempts=False,
    ):
        key = self.key("model:" + role)
        if agent is not None:
            task_id = self.task_id.get()
            current = agent.state.middle_context.get("recovery_feedback", {}).get(
                task_id, []
            )
            requested = self.snapshot.feedback_by_task.get(task_id, [])
            record = await self.store.operation_record(self.lease, key)
            target = current
            if record and record["state"] == "committed":
                frame = (record["result"] or {}).get("agent_state") or {}
                target = (
                    frame.get("middle_context", {})
                    .get("recovery_feedback", {})
                    .get(task_id, current)
                )
            elif record is None:
                target = requested
            if target != current:
                feedback = UserMsg(
                    "user",
                    "任务反馈：\n" + "\n".join(target[len(current) :]),
                    metadata={"research_protected": True},
                )
                messages.append(feedback)
                agent.state.context.append(feedback.model_copy(deep=True))
                agent.state.middle_context.setdefault("recovery_feedback", {})[
                    task_id
                ] = list(target)
        payload = {
            "role": role,
            "messages": stable_input(messages),
            "schema": schema.model_json_schema() if schema else None,
            "request": stable_input(request_details or {}),
        }
        # UTF-8 size includes tool schemas; actual usage remains provider-reported.
        import json

        reserve = {
            "model_calls": 1,
            "input_tokens": len(
                json.dumps(payload, ensure_ascii=False).encode("utf-8")
            ),
            "output_tokens": max_tokens,
        }
        budget = await self.store.budget(self.lease.run_id, self.lease.user_id)
        if self.model_accounting == "local" and "cost_micro_usd" in budget["limits"]:
            if pricing is None:
                raise ValueError(
                    "cost-capped model calls require an explicit priced reservation"
                )
            import math

            reserve["cost_micro_usd"] = math.ceil(
                reserve["input_tokens"] * pricing[0] + max_tokens * pricing[1]
            )

        async def invoke_response():
            response = await call()
            if not isinstance(response, (ChatResponse, StructuredResponse)):
                final = None
                async with aclosing(response) as stream:
                    async for chunk in stream:
                        if chunk.is_last:
                            final = chunk
                if final is None:
                    raise RuntimeError("model ended without a committed final response")
                response = final
            if response.finished_reason == FinishedReason.INTERRUPTED:
                raise asyncio.CancelledError()
            return {
                "codec": "agentscope-response-v1",
                "framework": "2.0.8",
                "response": response_dump(response),
                "agent_state": agent.state.model_dump(mode="json") if agent else None,
                "usage_details": _usage_details(response, agent),
            }

        async def invoke():
            from open_deep_research.agentscope_runtime.gateway import (
                gateway_operation_scope,
            )
            from open_deep_research.agentscope_runtime.model_accounting import (
                AttemptAccounting,
                current_accounting,
            )

            accounting = (
                AttemptAccounting(self, key, reserve, pricing)
                if account_attempts and self.model_accounting == "local" else None
            )
            token = current_accounting.set(accounting)
            try:
                with gateway_operation_scope(key):
                    result = await invoke_response()
                if accounting is not None and not accounting.ordinal:
                    raise RuntimeError("native model bypassed physical accounting policy")
                return result
            finally:
                current_accounting.reset(token)

        def settle(result):
            if self.model_accounting == "gateway" or account_attempts:
                return {}
            usage = result["response"].get("usage")
            details = result.get("usage_details") or {}
            failures = [
                item.get("billed_usage")
                for item in details.get("attempt_failures") or []
                if item and item.get("billed_usage")
            ]
            attempts = int(details.get("physical_attempts") or 0)
            if not usage:
                if attempts > 1 or failures:
                    # 未知用量保持保守预留；物理尝试数仍按实际情况计数。
                    return {**reserve, "model_calls": max(1, attempts)}
                return reserve
            actual = {
                "model_calls": max(1, attempts or 1),
                "input_tokens": usage["input_tokens"]
                + sum(item["input_tokens"] for item in failures),
                "output_tokens": usage["output_tokens"]
                + sum(item["output_tokens"] for item in failures),
            }
            if "cost_micro_usd" in reserve:
                import math

                actual["cost_micro_usd"] = math.ceil(
                    actual["input_tokens"] * pricing[0]
                    + actual["output_tokens"] * pricing[1]
                )
            return actual

        result = await self.operation(
            "model:" + role,
            payload,
            invoke,
            key=key,
            replay_safe=self.model_accounting == "gateway" or account_attempts,
            reserve=reserve if self.model_accounting == "local" and not account_attempts else {},
            actual=settle,
        )
        if (
            result.get("codec") != "agentscope-response-v1"
            or result.get("framework") != "2.0.8"
        ):
            raise ValueError("unsupported model receipt version")
        if agent is not None and result.get("agent_state") is not None:
            agent.state = AgentState.model_validate(result["agent_state"])
        return response_load(result["response"], structured=schema is not None)

    async def tool(self, tool, call_id, arguments, handler, *, bill=True):
        key = f"{self.stage.get()}:{self.task_id.get()}:tool:{call_id}"
        rejected_before_execution = {
            "permission_denied",
            "validation_error",
            "egress_domain_denied",
            "budget_exhausted",
            "deadline_exceeded",
        }

        async def invoke():
            result = await handler()
            if result.error and result.error.error_type.value in {
                "sensitive_tool_approval_required",
                "egress_domain_pending",
            }:
                # Governance rejected before executing the tool: release reservation.
                await self.store.resolve_operation(self.lease, key, not_executed=True)
                kind = (
                    "tool"
                    if result.error.error_type.value
                    == "sensitive_tool_approval_required"
                    else "egress"
                )
                action_id = digest([self.lease.run_id, key, kind])
                raise ApprovalPending(
                    action_id,
                    kind,
                    {
                        "operation_key": key,
                        "call_id": call_id,
                        "tool_name": tool.name,
                        "arguments_digest": digest(arguments),
                        "detail": result.error.detail,
                    },
                )
            if (
                result.error
                and not safe
                and result.error.error_type.value not in rejected_before_execution
            ):
                # A failed response does not prove that an external write failed.
                await self.store.begin_operation(
                    self.lease, key, "tool", {"name": tool.name, "arguments": arguments}
                )
            output = result.result.output if result.result else None
            if isinstance(output, BaseModel):
                output = output.model_dump(mode="json")
            return {
                "message": asdict(result.message),
                "output": output,
                "has_result": result.result is not None,
                "error": result.error.model_dump(mode="json") if result.error else None,
                "tool_metadata": (
                    dict(result.result.metadata)
                    if result.result is not None and result.result.metadata
                    else None
                ),
            }

        safe = tool.effect is ToolEffect.READ_ONLY or tool.supports_idempotency
        # 远区派发的工具由 Gateway 侧统一计费；宿主回执不再重复预留 tool_calls。
        reserve = {"tool_calls": 1} if bill else None
        if bill and tool.name in {"fetch_url", "fetch_webpage", "web_research"}:
            reserve["fetch_calls"] = 1

        def settle(result):
            if not bill:
                return None
            if (result.get("error") or {}).get("error_type") in (
                rejected_before_execution
            ):
                return {dimension: 0 for dimension in reserve}
            actual = dict(reserve)
            metadata = result.get("tool_metadata") or {}
            physical = metadata.get("physical_fetches")
            if "fetch_calls" in actual and physical is not None:
                # 实测物理抓取数；缺失时保持预留值，不当作零。
                actual["fetch_calls"] = int(physical)
            return actual

        value = await self.operation(
            "tool",
            {"name": tool.name, "arguments": arguments},
            invoke,
            key=key,
            replay_safe=safe,
            reserve=reserve,
            actual=settle,
        )
        return GovernedToolCallResult(
            ToolOutcomeMessage(**value["message"]),
            ToolResult(output=value["output"]) if value["has_result"] else None,
            ToolError.model_validate(value["error"]) if value["error"] else None,
        )

    def config(self, provider):
        return provider()

    def tool_config(self, config, tool_name, call_id, arguments):
        approved = []
        for grant in self.grants.values():
            payload = grant.get("payload", {})
            if (
                grant.get("kind") == "tool"
                and payload.get("operation_key")
                == f"{self.stage.get()}:{self.task_id.get()}:tool:{call_id}"
                and payload.get("call_id") == call_id
                and payload.get("tool_name") == tool_name
                and payload.get("arguments_digest") == digest(arguments)
            ):
                approved.append(call_id)
        return {
            **config,
            "metadata": {
                **config.get("metadata", {}),
                "approved_sensitive_tool_call_ids": approved,
            },
        }

    async def consume_decisions(self, pipeline, *, locked=False):
        for command in await self.store.pending_decisions(self.lease):
            self.current_command = command["command_id"]
            try:
                payload = command["payload"]
                if payload["action"] == "feedback":
                    state = pipeline.state.model_copy(deep=True)
                    state.feedback_by_task.setdefault(payload["task_id"], []).append(
                        payload["feedback"]
                    )
                    await pipeline._commit(state)
                    continue
                approval = pipeline.state.approvals.get(command["action_id"])
                if approval and payload["action"] == "approve":
                    if approval["kind"] == "budget":
                        if not payload.get("limits"):
                            raise ValueError(
                                "budget approval requires explicit new limits"
                            )
                        self.current_limits = payload["limits"]
                    elif approval["kind"] == "egress":
                        if self.approval_applier is None:
                            raise ValueError(
                                "egress approval requires its authority adapter"
                            )
                        # Adapter must use action_id as its durable idempotency key.
                        await self.approval_applier(
                            command["action_id"], approval["payload"]
                        )
                await (pipeline._decide if locked else pipeline.decide)(
                    command["action_id"], payload["action"], payload.get("feedback", "")
                )
            finally:
                self.current_command = None
                self.current_limits = None


class JournalMiddleware(MiddlewareBase):
    def __init__(self, session, role, max_tokens, pricing=None, *, account_attempts=False):
        self.session, self.role, self.max_tokens = session, role, max_tokens
        self.pricing = pricing
        self.account_attempts = account_attempts

    async def on_model_call(self, agent, input_kwargs, next_handler):
        return await self.session.model(
            self.role,
            input_kwargs["messages"],
            lambda: next_handler(**input_kwargs),
            max_tokens=self.max_tokens,
            pricing=self.pricing,
            request_details={
                k: v
                for k, v in input_kwargs.items()
                if k not in {"messages", "current_model"}
            },
            agent=agent,
            account_attempts=self.account_attempts,
        )


class RecoveryStages:
    """Wrap stage execution and lease renewal, keeping AgentScope loops native."""

    def __init__(self, inner, session):
        self.inner, self.session = inner, session

    async def execute(self, stage, state):
        current = asyncio.current_task()
        failure = []

        async def renew():
            try:
                while True:
                    await asyncio.sleep(self.session.ttl / 3)
                    await self.session.store.renew(self.session.lease, self.session.ttl)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - fence loss cancels the current executor
                failure.append(exc)
                current.cancel()

        heartbeat = asyncio.create_task(renew())
        try:
            with self.session.scope(stage, state.revision_count):
                external_effect = (
                    stage == "memory_extract_and_write"
                    and getattr(self.inner, "write_memory", None) is not None
                ) or (
                    stage == "final_report_generation"
                    and getattr(self.inner, "report_writer", None) is not None
                    and not getattr(self.inner.report_writer, "resumable", False)
                )
                if external_effect:
                    # These application ports may commit outside our SQL transaction.
                    # An unknown outcome must be reconciled, never blindly retried.
                    async def execute_external():
                        await self.inner.execute(stage, state)
                        return state.model_dump(mode="json")

                    receipt = await self.session.operation(
                        "stage_port",
                        state.model_dump(
                            mode="json",
                            exclude={"status", "inflight", "error", "reply_id"},
                        ),
                        execute_external,
                    )
                    restored = type(state).model_validate(receipt)
                    for name in type(state).model_fields:
                        setattr(state, name, getattr(restored, name))
                    result = None
                else:
                    result = await self.inner.execute(stage, state)
                if self.session.problem:
                    raise self.session.problem
                return result
        except asyncio.CancelledError:
            if failure:
                raise failure[0]
            raise
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
