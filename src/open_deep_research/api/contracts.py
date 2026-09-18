"""Existing browser request contracts shared by both engine adapters."""

from typing import Any, Literal

from pydantic import BaseModel, Field

from open_deep_research.documents.contracts import SourceSelection
from open_deep_research.report.models import PublisherTheme


class RunRequest(BaseModel):
    """HTTP request body for a research run."""

    messages: list[dict[str, Any]]
    configurable: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    title: str | None = Field(default=None, max_length=160)
    source_selection: SourceSelection = Field(default_factory=SourceSelection)
    publication_theme: PublisherTheme | None = None


class ResumeRunRequest(BaseModel):
    """Runtime overrides and credentials for explicitly resuming a run."""

    configurable: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class HumanActionRequest(BaseModel):
    """Approval/revision/cancellation response for a pending HITL action."""

    action: Literal["approve", "revise", "answer", "deny", "cancel"]
    message: str | None = None


class SecurityApprovalDecisionRequest(BaseModel):
    """Resolve one sandbox security approval without changing permanent policy."""

    decision: Literal["allow_once", "allow_run", "deny"]
    reason: str = Field(default="", max_length=1000)


class EgressModeChangeRequest(BaseModel):
    """Switch the run's runtime egress approval mode within the baseline."""

    mode: Literal["manual", "auto", "open"]
    reason: str = Field(default="", max_length=1000)


class HumanFeedbackRequest(BaseModel):
    """Mid-run human direction or evidence follow-up."""

    type: Literal["direction", "evidence_question"]
    message: str
    task_id: str | None = None
    source_url: str | None = None
    claim_text: str | None = None
    command_id: str | None = None


class PublicationRequest(BaseModel):
    """Create one idempotent publication for a completed run."""

    format: str = Field(min_length=1, max_length=40)
    theme: PublisherTheme | None = None


class EgressTargetDecisionRequest(BaseModel):
    """A versioned human override for an observed network target."""

    decision: Literal["allow_run", "block_run", "revoke"]
    reason: str = Field(default="", max_length=1000)
    expected_version: int = Field(ge=0)


class TeamMessageRequest(BaseModel):
    """Human direction to a member, scoped to the authenticated run owner."""

    to: str
    message: str | dict
    summary: str = ""
    command_id: str = Field(min_length=1, max_length=256)
