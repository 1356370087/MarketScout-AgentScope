"""SpawnTeammate protocol and tool definition."""

from pydantic import BaseModel, Field

from open_deep_research.tools.team.definitions import build_team_tool

from .prompt import DESCRIPTION, GUIDANCE


class SpawnInput(BaseModel):
    """Create a persistent research teammate."""
    name: str = Field(min_length=1, max_length=100)
    purpose: str = Field(min_length=1, max_length=4000)

def build(deps):
    """Build the role-scoped team operation."""
    schema = SpawnInput
    return build_team_tool('SpawnTeammate', schema, DESCRIPTION, GUIDANCE, deps)
