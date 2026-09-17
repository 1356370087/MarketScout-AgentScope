"""Knowledge-base subsystem: libraries, collections and material links."""

from __future__ import annotations

__all__ = [
    "batch_router",
    "fact_wiki_router",
    "router",
    "search_router",
    "workspace_router",
]


def __getattr__(name):
    """Keep domain imports independent of HTTP authentication and router setup."""
    if name not in __all__:
        raise AttributeError(name)
    from importlib import import_module
    exports = {key: import_module(f"{__name__}.{key}").router for key in __all__}
    globals().update(exports)
    return exports[name]
