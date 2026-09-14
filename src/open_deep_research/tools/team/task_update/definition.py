"""TaskUpdate protocol and tool definition."""

from typing import Literal

from pydantic import BaseModel, Field

from open_deep_research.tools.team.definitions import build_team_tool

from .prompt import DESCRIPTION, GUIDANCE


class TaskUpdateInput(BaseModel):
    """Claim a task or add dependency edges before it starts."""
    task_id: str
    action: Literal["claim", "dependencies", "instructions", "request_completion"]
    owner: str | None = None
    blocked_by: list[str] = Field(default_factory=list)
    instruction: str = Field(default="", max_length=12000)

def build(deps):
    """Build the role-scoped team operation."""
    schema = TaskUpdateInput
    return build_team_tool('TaskUpdate', schema, DESCRIPTION, GUIDANCE, deps)
