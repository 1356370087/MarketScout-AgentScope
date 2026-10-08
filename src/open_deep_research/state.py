"""Structured domain outputs for native clarification and research planning."""


from typing import Literal

from pydantic import BaseModel, Field


###################
# Structured Outputs
###################
class Summary(BaseModel):
    """Research summary with key findings."""
    
    summary: str
    key_excerpts: str

class ClarifyWithUser(BaseModel):
    """Model for user clarification requests."""
    
    need_clarification: bool = Field(
        description="Whether the user needs to be asked a clarifying question.",
    )
    question: str = Field(
        description="A question to ask the user to clarify the report scope",
    )
    verification: str = Field(
        description="Verify message that we will start research after the user has provided the necessary information.",
    )

class PlannedRequirement(BaseModel):
    """An atomic obligation quoted verbatim from a user message."""

    source_text: str
    source_message_index: int = Field(default=0, ge=0)
    kind: Literal["factual", "process", "deliverable"]


class ResearchEntity(BaseModel):
    """A named research subject and a proposed first-party entry point."""

    name: str = Field(min_length=1)
    website: str = ""


class ResearchQuestion(BaseModel):
    """Research question and brief for guiding research."""
    
    research_brief: str = Field(
        description="A research question that will be used to guide the research.",
    )
    requirements: list[PlannedRequirement] = Field(default_factory=list,
        description="Atomic requirements quoted EXACTLY from user messages. Separate factual questions, source/execution constraints and output format. Never invent obligations from the brief.")
    source_intent: Literal["unrestricted", "official_only", "prefer_official", "explicit"] = "unrestricted"
    source_directive: str = Field(default="", description="Exact contiguous quote of the user's instruction about which sources to use. Empty for questions merely about websites; never infer a source restriction from the brief.")
    entities: list[ResearchEntity] = Field(default_factory=list,
        description="Research subjects named by the user, with proposed official website URLs; proposals require verification, not model confidence.")
