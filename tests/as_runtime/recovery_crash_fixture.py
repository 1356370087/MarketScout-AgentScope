"""Hard-exit worker for M6; invoked only with a test-owned database and marker."""

import asyncio
import os
import sys
from pathlib import Path

from open_deep_research.agentscope_runtime.recovery import RecoverySession
from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore


async def main():
    database, run_id, marker, crash_at = sys.argv[1:]
    store = RecoveryStore(database)
    session = await RecoverySession.open(store, run_id, "owner", ttl=0.2)

    async def crash(point):
        if point == crash_at:
            os._exit(73)

    session.failpoint = crash

    async def effect():
        with Path(marker).open("a", encoding="utf-8") as output:  # noqa: ASYNC230 - deliberate crash fixture fsync boundary
            output.write("effect\n")
            output.flush()
            os.fsync(output.fileno())
        return {"written": True}

    with session.scope("research", 0):
        await session.operation("write", {}, effect, replay_safe=False)


if __name__ == "__main__":
    asyncio.run(main())
