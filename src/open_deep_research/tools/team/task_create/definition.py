"""TaskCreate protocol and tool definition."""

from pydantic import BaseModel, Field

from open_deep_research.tools.team.definitions import build_team_tool

from .prompt import DESCRIPTION, GUIDANCE


class TaskCreateInput(BaseModel):
    """Create a research objective on the shared task list."""
    subject: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=12000)
    requirement_ids: list[str] = Field(default_factory=list)
    blocked_by: list[str] = Field(default_factory=list)

def build(deps):
    """Build the role-scoped team operation."""
    from open_deep_research.tools.supervisor.common import coverage_bound_input_schema
    schema = coverage_bound_input_schema(TaskCreateInput, deps.coverage_contract)
    return build_team_tool('TaskCreate', schema, DESCRIPTION, GUIDANCE, deps)
