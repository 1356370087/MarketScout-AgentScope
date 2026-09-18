"""Lead and member tools share one authenticated team service."""

from typing import Literal
from pydantic import BaseModel, Field, model_validator

from open_deep_research.agentscope_runtime.research_agents import (
    _control_tool,
    _Empty,
    _TaskId,
)
from open_deep_research.tasks.team_messages import SendMessageInput
from open_deep_research.tasks.team_protocol import MemberIdentity
from open_deep_research.tools.base import ToolResult


class TeamCreateInput(BaseModel):
    name: str
    description: str = ""
    execution_mode: Literal["direct", "plan_approval"] | None = None


class SpawnInput(BaseModel):
    name: str
    purpose: str
    execution_mode: Literal["direct", "plan_approval"] | None = None


class TaskCreateInput(BaseModel):
    research_topic: str = ""
    subject: str | None = None
    description: str | None = None
    activeForm: str | None = None
    metadata: dict = Field(default_factory=dict)
    requirement_ids: list[str] = Field(
        min_length=1,
        description="从 task_requirement_choices 选择本任务负责的完整事实 ID；汇总任务复用上游的事实 ID，不填写 process/deliverable 的 ID。",
    )
    blockedBy: list[str] = Field(description="创建时原子设置前置任务 ID；无依赖显式填 []，不要创建后再补依赖。")
    owner: str | None = Field(description='派发时填写 ListAgents 返回的成员 ID；自主认领任务填空字符串 ""，运行时保存为未分配。不要填写字符串 "null"。')
    proposal_id: str | None = None

    @classmethod
    def model_json_schema(cls, **kwargs):
        schema = super().model_json_schema(**kwargs)
        # Keep the model-facing value a string: the configured model emits
        # literal "null" for either nullable schema form. Empty maps to SQL NULL.
        owner = schema["properties"]["owner"]
        owner.pop("anyOf")
        owner["type"] = "string"
        return schema

    @model_validator(mode="after")
    def task_description(self):
        self.research_topic = (
            self.description or self.research_topic or self.subject or ""
        )
        if not self.research_topic.strip():
            raise ValueError("task_description_required")
        if self.owner == "null":
            raise ValueError('自主认领请传空字符串 owner=""，不能传字符串 "null"。')
        if self.owner == "":
            self.owner = None
        return self


class TaskUpdateInput(BaseModel):
    task_id: str
    version: int
    owner: str | None = None
    subject: str | None = None
    description: str | None = None
    activeForm: str | None = None
    metadata: dict = Field(default_factory=dict)
    addBlocks: list[str] = Field(default_factory=list)
    addBlockedBy: list[str] = Field(default_factory=list)
    removeBlocks: list[str] = Field(default_factory=list)
    removeBlockedBy: list[str] = Field(default_factory=list)


class ProposalDecision(BaseModel):
    proposal_id: str
    reason: str


class SendMessageTool:
    """Trusted sender binding for the model-visible SendMessage tool."""

    def __init__(self, team, identity, prefix):
        self.team, self.identity, self.prefix = team, identity, prefix

    async def call(self, input, context, progress):
        result = await self.team.send_message(
            self.identity,
            self.prefix + context.tool_call_id,
            input.to,
            input.message,
            input.summary,
        )
        return ToolResult(
            output={
                **result,
                "body_type": "text" if isinstance(input.message, str) else "structured",
                "protocol_notice": "字符串仅作为文本投递，不批准计划、不关闭成员。控制操作必须传 message 对象；plan_approval_response 只包含 type/task_id/request_id/version/approve/feedback，不含 content。",
            }
        )


def communication_tools(team, identity, prefix):
    async def members(input, context, progress):
        return ToolResult(output=await team.members())

    sender = SendMessageTool(team, identity, prefix)
    return [
        _control_tool("SendMessage", SendMessageInput, sender.call, idempotent=True),
        _control_tool("ListAgents", _Empty, members, idempotent=True),
    ]


def member_tools(worker):
    team = worker.team
    identity = MemberIdentity(
        run_id=team.lease.run_id, member_id=worker.member_id, name=worker.member_id
    )
    tools = communication_tools(team, identity, "member-send:" + worker.task_id + ":")

    async def tasks(input, context, progress):
        from open_deep_research.tasks.team_service import task_view

        rows = await team.service.tasks(team.lease.run_id)
        if hasattr(input, "task_id"):
            return ToolResult(
                output=task_view(
                    next(row for row in rows if row["task_id"] == input.task_id)
                )
            )
        return ToolResult(output=[task_view(row, summary=True) for row in rows])

    tools += [
        _control_tool("TaskList", _Empty, tasks, idempotent=True),
        _control_tool("TaskGet", _TaskId, tasks, idempotent=True),
    ]
    return tools


def lead_tools(host, cfg):
    team = host.team

    async def create(input, context, progress):
        team_id = await team.create(
            input.name,
            input.description,
            mode="teams",
            execution_mode=input.execution_mode or cfg.team_execution_mode,
        )
        return ToolResult(output={"team_id": team_id})

    async def spawn(input, context, progress):
        member = await team.add_member(
            "spawn:" + context.tool_call_id,
            input.name,
            input.purpose,
            max_members=cfg.max_concurrent_research_units,
            execution_mode=input.execution_mode,
        )
        await host.ensure_members()
        return ToolResult(output={"member_id": member})

    async def update(input, context, progress):
        value = input.model_dump(exclude_none=True)
        if input.owner:
            # Assignment and graph edits are explicit separate operations, each CAS guarded.
            if any(
                value.get(k)
                for k in (
                    "addBlocks",
                    "addBlockedBy",
                    "removeBlocks",
                    "removeBlockedBy",
                )
            ):
                raise ValueError("assign_and_dependency_edit_require_separate_versions")
            return ToolResult(
                output=await team.command(
                    "assign:" + context.tool_call_id, "task_assign", value
                )
            )
        return ToolResult(
            output=await team.command(
                "update:" + context.tool_call_id, "task_update", value
            )
        )

    async def reject(input, context, progress):
        return ToolResult(
            output=await team.command(
                "proposal:" + context.tool_call_id,
                "proposal_reject",
                input.model_dump(),
            )
        )

    return [
        _control_tool("TeamCreate", TeamCreateInput, create, idempotent=True),
        _control_tool("SpawnTeammate", SpawnInput, spawn, idempotent=True),
        _control_tool("TaskUpdate", TaskUpdateInput, update, idempotent=True),
        _control_tool("RejectTaskProposal", ProposalDecision, reject, idempotent=True),
        *communication_tools(team, team.leader, "lead-send:"),
    ]
