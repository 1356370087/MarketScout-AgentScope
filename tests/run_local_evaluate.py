"""Compatibility entry point; implementation lives in the evaluation package."""

import sys

from open_deep_research.evaluation import legacy_cli as _implementation

if __name__ == "__main__":
    import asyncio
    asyncio.run(_implementation.main())
else:
    sys.modules[__name__] = _implementation
