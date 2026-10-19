"""Lead and member tools share one authenticated team service."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from open_deep_research.agentscope_runtime.research_agents import (
    _control_tool,
    _Empty,
    _TaskId,
    _Topic,
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
    model_config = ConfigDict(extra="forbid")
    research_topic: str = Field(default="", exclude=True)
    subject: str = Field(min_length=1, description='简短、明确的祈使句标题，例如“核实市场规模”。')
    description: str = Field(min_length=1, description=_Topic.model_fields["research_topic"].description)
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
        schema["properties"].pop("research_topic", None)
        # Keep the model-facing value a string: the configured model emits
        # literal "null" for either nullable schema form. Empty maps to SQL NULL.
        owner = schema["properties"]["owner"]
        owner.pop("anyOf")
        owner["type"] = "string"
        return schema

    @model_validator(mode="before")
    @classmethod
    def legacy_task_fields(cls, value):
        if not isinstance(value, dict):
            return value
        value = dict(value)
        legacy = value.get("research_topic")
        if legacy and value.get("description") and legacy != value["description"]:
            raise ValueError("conflicting_task_descriptions")
        # Previous frozen tool calls used research_topic or a subject-only input.
        if "description" not in value:
            value["description"] = legacy or value.get("subject", "")
        if "subject" not in value:
            value["subject"] = legacy or value.get("description", "")
        return value

    @field_validator("subject", "description")
    @classmethod
    def nonempty_content(cls, value):
        if not value.strip():
            raise ValueError("task_content_required")
        return value

    @model_validator(mode="after")
    def task_description(self):
        self.research_topic = self.description
        if self.owner == "null":
            raise ValueError('自主认领请传空字符串 owner=""，不能传字符串 "null"。')
        if self.owner == "":
            self.owner = None
        return self


class TaskUpdateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    version: int = Field(ge=1)
    owner: str | None = Field(default=None, description='不传则保留负责人；传成员 ID 或姓名进行分配；空字符串或 null 将待办任务放回公共任务池。')
    subject: str | None = None
    description: str | None = None
    activeForm: str | None = None
    metadata: dict = Field(default_factory=dict)
    addBlocks: list[str] = Field(default_factory=list)
    addBlockedBy: list[str] = Field(default_factory=list)
    removeBlocks: list[str] = Field(default_factory=list, description="移除反向依赖；未解除的阻塞必须为同一下游在本次调用中加入替代前置，否则事务拒绝。补证优先更新下游的 removeBlockedBy/addBlockedBy。")
    removeBlockedBy: list[str] = Field(default_factory=list, description="移除前置依赖；尚未完成且质量接纳的前置不可单独删除。先创建补证任务，再在同一次调用中 addBlockedBy 新任务并 removeBlockedBy 旧任务。")

    @field_validator("subject", "description")
    @classmethod
    def nonempty_content(cls, value):
        if value is not None and not value.strip():
            raise ValueError("task_content_required")
        return value

    @field_validator("owner")
    @classmethod
    def normalize_owner(cls, value):
        if value == "null":
            raise ValueError('解除分配请传空字符串或 null，不能传字符串 "null"。')
        return value or None


class ProposalDecision(BaseModel):
    proposal_id: str
    reason: str


def dependency_synthesis_ids(input, tasks, coverage):
    """Permit a synthesis only within the factual scope of its predecessors."""
    if not input.blockedBy:
        return None
    from open_deep_research.quality.contract import canonicalize_requirement_ids, validate_requirement_ids

    by_id = {task["task_id"]: task for task in tasks}
    if any(task_id not in by_id for task_id in input.blockedBy):
        raise ValueError("dependency_task_not_found")
    predecessor_ids = {rid for task_id in input.blockedBy for rid in by_id[task_id]["requirement_ids"]}
    ids = canonicalize_requirement_ids(input.requirement_ids, coverage)
    if not ids or not set(ids).issubset(predecessor_ids):
        return None
    return validate_requirement_ids(ids, coverage, required=True, allow_cross_dimension=True)


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
        return ToolResult(
            output=await team.command(
                "update:" + context.tool_call_id, "task_update", input.model_dump(exclude_unset=True)
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
