"""TeamCreate protocol and tool definition."""

from pydantic import BaseModel, Field

from open_deep_research.tools.team.definitions import build_team_tool

from .prompt import DESCRIPTION, GUIDANCE


class TeamCreateInput(BaseModel):
    """Create one team for this research run."""
    name: str = Field(min_length=1, max_length=100)

def build(deps):
    """Build the role-scoped team operation."""
    schema = TeamCreateInput
    return build_team_tool('TeamCreate', schema, DESCRIPTION, GUIDANCE, deps)
