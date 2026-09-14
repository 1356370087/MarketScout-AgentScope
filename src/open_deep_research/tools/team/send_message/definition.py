"""SendMessage protocol and tool definition."""

from pydantic import BaseModel, Field

from open_deep_research.tools.team.definitions import build_team_tool

from .prompt import DESCRIPTION, GUIDANCE


class MessageInput(BaseModel):
    """Send a bounded message to a teammate or all current teammates."""
    to: str
    message: str = Field(min_length=1, max_length=12000)
    summary: str = Field(default="", max_length=200)

def build(deps):
    """Build the role-scoped team operation."""
    schema = MessageInput
    return build_team_tool('SendMessage', schema, DESCRIPTION, GUIDANCE, deps)
