"""Typed team messages: text never impersonates a control protocol."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class MessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskProposal(MessageBody):
    type: Literal["task_proposal"]
    subject: str
    description: str
    requirement_ids: list[str] = Field(default_factory=list)
    blockedBy: list[str] = Field(default_factory=list)


class ResearchPlan(MessageBody):
    objective: str
    requirement_ids: list[str]
    steps: list[str]
    sources: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    budget: str
    acceptance_criteria: list[str]


class PlanRequest(MessageBody):
    type: Literal["plan_approval_request"]
    task_id: str
    plan: ResearchPlan


class PlanResponse(MessageBody):
    type: Literal["plan_approval_response"]
    task_id: str
    request_id: str
    version: int
    approve: bool
    feedback: str = Field(min_length=1)


class EvidenceShare(MessageBody):
    type: Literal["evidence_share", "help_request", "task_result", "task_assignment"]
    task_id: str
    content: str
    artifact_refs: list[str] = Field(default_factory=list)


class ShutdownRequest(MessageBody):
    type: Literal["shutdown_request"]
    reason: str = "研究完成"


class ShutdownResponse(MessageBody):
    type: Literal["shutdown_response"]
    request_id: str
    approve: bool
    reason: str = ""


StructuredMessage = Annotated[
    TaskProposal
    | PlanRequest
    | PlanResponse
    | EvidenceShare
    | ShutdownRequest
    | ShutdownResponse,
    Field(discriminator="type"),
]
message_adapter = TypeAdapter(StructuredMessage)


class SendMessageInput(MessageBody):
    to: str
    summary: str = ""
    message: StructuredMessage | str = Field(
        description=(
            "普通讨论传纯文本字符串。计划审批等控制消息必须传对象，不得 JSON.stringify。"
            '例如 {"type":"plan_approval_response","task_id":"任务ID",'
            '"request_id":"请求事件ID","version":1,"approve":false,"feedback":"修改理由"}。'
            "字符串只会投递为聊天文本，不能批准或驳回计划。"
        ),
    )

    @classmethod
    def model_json_schema(cls, **kwargs):
        """Project a flat tool schema; runtime validation retains the typed union.

        Some configured model routes stringify nested anyOf/oneOf arguments.
        A JSON Schema type array expresses the same text/object boundary without
        that nesting. Per-message required fields remain enforced by Pydantic.
        """
        schema = super().model_json_schema(**kwargs)
        definitions = schema.pop("$defs")

        def expand(value):
            if isinstance(value, list):
                return [expand(item) for item in value]
            if not isinstance(value, dict):
                return value
            if "$ref" in value:
                return expand(definitions[value["$ref"].rsplit("/", 1)[1]])
            return {
                key: expand(item)
                for key, item in value.items()
                if key != "discriminator"
            }

        message = expand(schema["properties"]["message"])
        variants = next(
            branch["oneOf"] for branch in message["anyOf"] if "oneOf" in branch
        )
        properties = {
            key: value
            for branch in variants
            for key, value in branch["properties"].items()
        }
        kinds = []
        for branch in variants:
            kind = branch["properties"]["type"]
            kinds.extend(kind.get("enum", [kind.get("const")]))
        properties["type"] = {"type": "string", "enum": kinds}
        schema["properties"]["message"] = {
            "type": ["object", "string"],
            "properties": properties,
            "required": ["type"],
            "additionalProperties": False,
            "description": message["description"],
        }
        return schema


def message_text(event):
    """Produce labeled input without interpreting ordinary text as JSON."""
    import json

    body = event.payload.get("message", event.payload.get("content", ""))
    return body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
