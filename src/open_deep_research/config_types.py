"""Project-owned configuration mapping passed across application boundaries.

This is not an executable framework object; AgentScope uses RunConfig internally.
"""

from typing import Any

type RuntimeConfig = dict[str, Any]
