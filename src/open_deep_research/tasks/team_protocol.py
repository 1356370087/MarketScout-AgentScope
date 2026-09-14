"""Durable team identities and transport-independent coordination events."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


class CoordinationUnavailable(RuntimeError):
    """Interrupt the run rather than converting a durable-state failure to research failure."""


class MemberIdentity(BaseModel):
    """Authenticated identity supplied by the host, never by tool arguments."""

    run_id: str
    member_id: str
    name: str
    role: Literal["lead", "researcher"] = "researcher"


member_identity: ContextVar[MemberIdentity | None] = ContextVar(
    "research_member_identity", default=None,
)


class TeamEvent(BaseModel):
    """Small business event shared by PostgreSQL and RocketMQ."""

    schema_version: Literal[1] = 1
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    operation_id: str
    run_id: str
    sender: str
    recipients: list[str]
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    fence_token: int = 0

    @property
    def is_control(self) -> bool:
        """Keep cancellation traffic independent from research chatter."""
        return self.type in {
            "cancel_request", "task_stop", "shutdown_request", "shutdown_response", "shutdown_ack",
        }
