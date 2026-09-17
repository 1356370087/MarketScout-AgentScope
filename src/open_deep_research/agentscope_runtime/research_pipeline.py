"""Research stages implementing AgentScope's public PipelineProtocol.

Stage checkpoints are committed before publication. An unfinished stage found
after a crash requires reconciliation rather than replaying unknown side effects.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from agentscope.event import (
    ReplyEndEvent,
    ReplyStartEvent,
    RequireUserConfirmEvent,
    UserConfirmResultEvent,
    UserInterruptEvent,
)
from agentscope.message import AssistantMsg, Msg, ToolCallBlock, UserMsg
from agentscope.middleware import MiddlewareBase
from agentscope.types import ReplyFinishedReason
from pydantic import BaseModel, Field, model_validator

STAGES = (
    "summarize_messages",
    "memory_recall",
    "clarify_with_user",
    "write_research_brief",
    "plan_approval",
    "research_supervisor",
    "outline_approval",
    "final_report_generation",
    "memory_extract_and_write",
)


class PendingDecision(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    stage: str
    question: str


class ResearchSnapshot(BaseModel):
    """JSON-native research state, separate from individual AgentState objects."""

    version: Literal[1] = 1
    framework_version: Literal["2.0.8"] = "2.0.8"
    engine: Literal["agentscope"] = "agentscope"
    run_id: str
    config_fingerprint: str
    reply_id: str = Field(default_factory=lambda: uuid4().hex)
    status: Literal[
        "ready", "running", "waiting", "completed", "failed", "cancelled"
    ] = "ready"
    completed: list[str] = Field(default_factory=list)
    inflight: str | None = None
    messages: list[Msg] = Field(default_factory=list)
    conversation_summary: str = ""
    memory_context: str = ""
    research_brief: str = ""
    coverage_contract: dict = Field(default_factory=dict)
    research_risk_profile: dict = Field(default_factory=dict)
    completion_outcome: dict = Field(default_factory=dict)
    coverage_ledger: dict = Field(default_factory=dict)
    findings: list[dict] = Field(default_factory=list)
    outline: str = ""
    final_report: str = ""
    report_product: dict = Field(default_factory=dict)
    report_date: str = Field(default_factory=lambda: datetime.now(UTC).date().isoformat())
    pending: PendingDecision | None = None
    decisions: dict[str, str] = Field(default_factory=dict)
    feedback: list[str] = Field(default_factory=list)
    agent_states: dict = Field(default_factory=dict)
    application: dict = Field(default_factory=dict)
    error: str | None = None
    revision_count: int = 0
    approvals: dict = Field(default_factory=dict)
    approval_grants: dict = Field(default_factory=dict)
    feedback_by_task: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_stage_prefix(self):
        if self.completed != list(STAGES[: len(self.completed)]):
            raise ValueError("research checkpoint stages are not a valid prefix")
        return self


class ResearchStages(Protocol):
    async def execute(
        self, stage: str, state: ResearchSnapshot
    ) -> PendingDecision | None:
        """Update a private stage snapshot; optionally park for a decision."""


class FileResearchCheckpoint:
    """Atomic per-run snapshots; cross-process ownership remains an M6 concern."""

    def __init__(self, path: Path):
        self.path = path

    async def save(self, state: ResearchSnapshot) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                stream.write(state.model_dump_json())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def load(self) -> ResearchSnapshot:
        return ResearchSnapshot.model_validate_json(
            self.path.read_text(encoding="utf-8")
        )


class ResearchPipeline:
    """Run stages, park approvals and resume without repeating committed stages."""

    def __init__(
        self,
        state: ResearchSnapshot,
        stages: ResearchStages,
        save: Callable[[ResearchSnapshot], Awaitable[None]],
        *,
        config_fingerprint: str,
        recovery=None,
    ):
        if state.config_fingerprint != config_fingerprint:
            raise ValueError("research configuration fingerprint changed")
        if state.inflight:
            raise ValueError("unfinished stage requires M6 reconciliation")
        self.state = state.model_copy(deep=True)
        self.stages = stages
        self.save = save
        self._lock = asyncio.Lock()
        self.recovery = recovery

    async def _commit(self, state: ResearchSnapshot) -> None:
        await self.save(state.model_copy(deep=True))
        self.state = state

    async def decide(self, decision_id: str, action: str, feedback: str = "") -> None:
        """Consume an exact pending decision; repeated identical decisions are no-ops."""
        async with self._lock:
            await self._decide(decision_id, action, feedback)

    async def _decide(self, decision_id: str, action: str, feedback: str = "") -> None:
        if action not in {"approve", "revise", "cancel", "answer"}:
            raise ValueError("unsupported research decision")
        receipt = f"{action}:{feedback}"
        if decision_id in self.state.decisions:
            if self.state.decisions[decision_id] != receipt:
                raise ValueError("conflicting research decision")
            return
        pending = self.state.pending
        if decision_id in self.state.approvals:
            approval = self.state.approvals[decision_id]
            if action not in {"approve", "revise", "cancel"}:
                raise ValueError("unsupported operation approval action")
            state = self.state.model_copy(deep=True)
            state.decisions[decision_id] = receipt
            del state.approvals[decision_id]
            if action == "approve":
                state.approval_grants[decision_id] = approval
            elif action == "cancel":
                state.status = "cancelled"
                state.approvals.clear()
            else:
                state.revision_count += 1
                state.feedback.append(feedback)
            if action != "cancel":
                state.status = "waiting" if state.approvals else "ready"
            await self._commit(state)
            return
        if pending is None or pending.id != decision_id:
            raise ValueError("stale research decision")
        if action == "answer" and pending.stage != "clarify_with_user":
            raise ValueError("answer is only valid for clarification")
        if action in {"answer", "revise"} and not feedback.strip():
            raise ValueError("decision requires feedback")
        if pending.stage == "clarify_with_user" and action == "approve":
            raise ValueError("clarification requires an answer")
        state = self.state.model_copy(deep=True)
        state.decisions[decision_id] = receipt
        state.pending = None
        if feedback:
            state.feedback.append(feedback)
            state.messages.append(UserMsg("user", feedback))
        if action == "cancel":
            state.status = "cancelled"
        elif action == "revise":
            state.revision_count += 1
            target = (
                "write_research_brief"
                if pending.stage in {"plan_approval", "clarify_with_user"}
                else "outline_approval"
            )
            state.completed = list(STAGES[: STAGES.index(target)])
            state.status = "ready"
        else:
            state.completed.append(pending.stage)
            state.status = "ready"
        await self._commit(state)

    def _pending_event(self):
        pending = self.state.pending
        return RequireUserConfirmEvent(
            reply_id=self.state.reply_id,
            tool_calls=[
                ToolCallBlock(
                    id=pending.id,
                    name=pending.stage,
                    input=json.dumps(
                        {"question": pending.question}, ensure_ascii=False
                    ),
                )
            ],
            metadata={"research_stage": pending.stage, "run_id": self.state.run_id},
        )

    async def reply_stream(self, inputs=None):
        async with self._lock:
            if isinstance(inputs, UserInterruptEvent):
                if inputs.reply_id != self.state.reply_id:
                    raise ValueError("interrupt targets another reply")
                state = self.state.model_copy(deep=True)
                state.status, state.pending = "cancelled", None
                await self._commit(state)
            elif isinstance(inputs, UserConfirmResultEvent):
                if inputs.reply_id != self.state.reply_id or not inputs.confirm_results:
                    raise ValueError("confirmation does not match research reply")
                for result in inputs.confirm_results:
                    action = inputs.metadata.get(
                        "action", "approve" if result.confirmed else "cancel"
                    )
                    if action == "approve" and not result.confirmed:
                        raise ValueError("approval contradicts native confirmation")
                    if self.recovery:
                        from open_deep_research.agentscope_runtime.recovery_store import (
                            digest,
                        )

                        payload = {
                            "action": action,
                            "feedback": inputs.metadata.get("feedback", ""),
                        }
                        if "limits" in inputs.metadata:
                            payload["limits"] = inputs.metadata["limits"]
                        command_id = inputs.metadata.get(
                            "command_id",
                            digest([self.state.reply_id, result.tool_call.id, payload]),
                        )
                        await self.recovery.store.submit_decision(
                            self.state.run_id,
                            self.recovery.lease.user_id,
                            command_id + ":" + result.tool_call.id,
                            result.tool_call.id,
                            payload,
                        )
                    else:
                        await self._decide(
                            result.tool_call.id,
                            action,
                            inputs.metadata.get("feedback", ""),
                        )
                if self.recovery:
                    await self.recovery.consume_decisions(self, locked=True)
            elif inputs is not None:
                messages = [inputs] if isinstance(inputs, Msg) else inputs
                if (
                    not isinstance(messages, list)
                    or not messages
                    or not all(
                        isinstance(m, Msg) and m.role == "user" for m in messages
                    )
                ):
                    raise ValueError("research inputs must be user messages")
                if (
                    self.state.pending
                    and self.state.pending.stage == "clarify_with_user"
                ):
                    await self._decide(
                        self.state.pending.id,
                        "answer",
                        "\n".join(m.get_text_content() for m in messages),
                    )
                elif not self.state.messages and not self.state.completed:
                    state = self.state.model_copy(deep=True)
                    state.messages = [m.model_copy(deep=True) for m in messages]
                    await self._commit(state)
                elif [m.id for m in messages] != [m.id for m in self.state.messages]:
                    raise ValueError(
                        "active research accepts only its pending decision"
                    )

            yield ReplyStartEvent(
                session_id=self.state.run_id,
                reply_id=self.state.reply_id,
                name="research",
            )
            if self.state.approvals:
                yield RequireUserConfirmEvent(
                    reply_id=self.state.reply_id,
                    tool_calls=[
                        ToolCallBlock(
                            id=key,
                            name=value["kind"],
                            input=json.dumps(value["payload"], ensure_ascii=False),
                        )
                        for key, value in self.state.approvals.items()
                    ],
                    metadata={"run_id": self.state.run_id},
                )
                return
            if self.state.pending:
                yield self._pending_event()
                yield AssistantMsg("research", self.state.pending.question)
                return
            if not self.state.messages and self.state.status not in {
                "cancelled",
                "failed",
            }:
                raise ValueError("research has no input")
            while len(self.state.completed) < len(STAGES) and self.state.status not in {
                "cancelled",
                "failed",
            }:
                if self.recovery:
                    await self.recovery.consume_decisions(self, locked=True)
                if self.state.status in {"cancelled", "failed"}:
                    break
                stage = STAGES[len(self.state.completed)]
                started = self.state.model_copy(deep=True)
                started.status, started.inflight = "running", stage
                await self._commit(started)
                working = self.state.model_copy(deep=True)
                try:
                    pending = await self.stages.execute(stage, working)
                    working.inflight = None
                    if pending:
                        working.pending, working.status = pending, "waiting"
                    else:
                        working.completed.append(stage)
                        working.status = (
                            "completed"
                            if len(working.completed) == len(STAGES)
                            else "ready"
                        )
                    await self._commit(working)
                except asyncio.CancelledError:
                    stopped = self.state.model_copy(deep=True)
                    stopped.status, stopped.error = (
                        ("ready", "process_interrupted")
                        if self.recovery
                        else ("cancelled", "execution_cancelled")
                    )
                    await self._commit(stopped)
                    raise
                except Exception as exc:
                    if self.recovery:
                        from open_deep_research.agentscope_runtime.recovery import (
                            ApprovalPending,
                        )

                        problem = self.recovery.problem or exc
                        if isinstance(problem, ApprovalPending):
                            working.inflight, working.status = None, "waiting"
                            working.approvals[problem.action_id] = {
                                "kind": problem.kind,
                                "payload": problem.payload,
                            }
                            await self._commit(working)
                            yield RequireUserConfirmEvent(
                                reply_id=working.reply_id,
                                tool_calls=[
                                    ToolCallBlock(
                                        id=problem.action_id,
                                        name=problem.kind,
                                        input=json.dumps(
                                            problem.payload, ensure_ascii=False
                                        ),
                                    )
                                ],
                            )
                            return
                    from open_deep_research.agentscope_runtime.research_agents import (
                        ResearchTerminated,
                    )

                    if isinstance(exc, ResearchTerminated):
                        working.status, working.inflight = "failed", None
                        working.error = exc.reason
                        working.completion_outcome = {
                            "action": "terminate",
                            "reason": exc.reason,
                            "gaps": exc.gaps,
                        }
                        working.agent_states["terminal_assessments"] = exc.outcomes
                        if exc.agent_state:
                            working.agent_states["supervisor"] = exc.agent_state
                        await self._commit(working)
                        raise
                    # Retain the durable inflight marker. A failed checkpoint or
                    # side effect must not be retried merely by reopening state.
                    self.state.status, self.state.error = "failed", type(exc).__name__
                    raise
                if pending:
                    yield self._pending_event()
                    yield AssistantMsg("research", pending.question)
                    return
            reason = (
                ReplyFinishedReason.COMPLETED
                if self.state.status == "completed"
                else ReplyFinishedReason.ERROR
                if self.state.status == "failed"
                else ReplyFinishedReason.INTERRUPTED
            )
            yield ReplyEndEvent(
                session_id=self.state.run_id,
                reply_id=self.state.reply_id,
                finished_reason=reason,
            )
            yield AssistantMsg(
                "research",
                self.state.final_report
                if self.state.status == "completed"
                else "研究已取消或停止。",
            )


class ResearchPipelineMiddleware(MiddlewareBase):
    """Service extension through create_app(extra_agent_middlewares=...)."""

    def __init__(self, pipeline: ResearchPipeline, gate=None):
        self.pipeline = pipeline
        self.gate = gate

    async def on_reply(self, agent, input_kwargs, next_handler):
        from contextlib import aclosing

        if self.gate:
            self.gate.begin()
        try:
            async with aclosing(
                self.pipeline.reply_stream(input_kwargs.get("inputs"))
            ) as stream:
                async for event in stream:
                    agent.state.middle_context["research"] = (
                        self.pipeline.state.model_dump(mode="json")
                    )
                    yield event
        finally:
            agent.state.middle_context["research"] = self.pipeline.state.model_dump(
                mode="json"
            )
            try:
                if self.pipeline.recovery:
                    await self.pipeline.recovery.close()
            finally:
                if self.gate:
                    await self.gate.end()
