"""Knowledge-base subsystem: libraries, collections and material links."""

from __future__ import annotations

from .batch_router import router as batch_router
from .fact_wiki_router import router as fact_wiki_router
from .router import router
from .search_router import router as search_router
from .workspace_router import router as workspace_router

__all__ = [
    "batch_router",
    "fact_wiki_router",
    "router",
    "search_router",
    "workspace_router",
]
