"""Trusted, task-local network authorization for nested Gateway fetches."""

from contextvars import ContextVar
from typing import Awaitable, Callable

EgressAuthorizer = Callable[[str, str, bool], Awaitable[str]]
egress_authorizer: ContextVar[EgressAuthorizer | None] = ContextVar(
    "egress_authorizer", default=None
)


async def authorize_url(url: str, capability: str = "tool.egress", consume: bool = False) -> str:
    """Ask the bound Gateway authority; callers enforce standalone policy."""
    authorizer = egress_authorizer.get()
    return await authorizer(url, capability, consume) if authorizer else "unbound"
