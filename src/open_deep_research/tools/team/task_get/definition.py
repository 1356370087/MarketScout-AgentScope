"""TaskGet protocol and tool definition."""

from pydantic import BaseModel

from open_deep_research.tools.team.definitions import build_team_tool

from .prompt import DESCRIPTION, GUIDANCE


class TaskGetInput(BaseModel):
    """Select a task in the current run."""
    task_id: str

def build(deps):
    """Build the role-scoped team operation."""
    schema = TaskGetInput
    return build_team_tool('TaskGet', schema, DESCRIPTION, GUIDANCE, deps)
