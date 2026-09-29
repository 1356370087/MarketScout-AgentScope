"""Compatibility entry point; implementation lives in the evaluation package."""

import sys

from open_deep_research.evaluation import evaluators as _implementation

sys.modules[__name__] = _implementation
