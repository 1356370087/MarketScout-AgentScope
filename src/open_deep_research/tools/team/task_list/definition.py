"""TaskList protocol and tool definition."""

from pydantic import BaseModel

from open_deep_research.tools.team.definitions import build_team_tool

from .prompt import DESCRIPTION, GUIDANCE


class EmptyInput(BaseModel):
    """Read or close the current team."""

def build(deps):
    """Build the role-scoped team operation."""
    schema = EmptyInput
    return build_team_tool('TaskList', schema, DESCRIPTION, GUIDANCE, deps)
