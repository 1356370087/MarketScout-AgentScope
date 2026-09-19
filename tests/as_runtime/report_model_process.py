"""Independent native writer paused after the durable model-result commit."""

import asyncio
import sys
from pathlib import Path

from test_report_native import Factory, config, state
from open_deep_research.agentscope_runtime.recovery import RecoverySession
from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
from open_deep_research.agentscope_runtime.report import NativeReportWriter
from open_deep_research.agentscope_runtime.research_models import ResearchModels


async def main(root):
    store = RecoveryStore("sqlite+aiosqlite:///" + (root / "journal.db").as_posix())

    class RecordedFactory(Factory):
        async def complete_with_recovery(self, role, messages, **kwargs):
            with (root / "model-calls").open("a", encoding="utf-8") as output:
                output.write(role + "\n")
            return await super().complete_with_recovery(role, messages, **kwargs)

    async def stop_after_commit(point):
        if point == "operation_committed":
            (root / "model-ready").write_text(point, encoding="utf-8")
            await asyncio.Event().wait()

    snapshot = state()
    session = await RecoverySession.open(store, snapshot.run_id, "owner", ttl=1, failpoint=stop_after_commit)
    try:
        with session.scope("final_report_generation", 0):
            await NativeReportWriter(ResearchModels(RecordedFactory(), recovery=session))(snapshot, config(root))
    finally:
        await session.close()
        await store.aclose()


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
