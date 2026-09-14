"""WaitForTeamEvents protocol and tool definition."""

from pydantic import BaseModel, Field

from open_deep_research.tools.team.definitions import build_team_tool

from .prompt import DESCRIPTION, GUIDANCE


class WaitInput(BaseModel):
    """Wait for team input without polling or invoking a model."""
    timeout_seconds: int = Field(default=30, ge=1, le=300)

def build(deps):
    """Build the role-scoped team operation."""
    schema = WaitInput
    return build_team_tool('WaitForTeamEvents', schema, DESCRIPTION, GUIDANCE, deps)
