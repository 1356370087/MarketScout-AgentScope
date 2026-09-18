"""Trusted team Worker entrypoint: python -m ...team_executor.

AS_TEAM_EXECUTION contains only the leader lease and task id. Credentials and
SQL settings come from the trusted deployment, never from task arguments.
"""

import asyncio
import json
import os
from contextlib import AsyncExitStack
from pathlib import Path

from open_deep_research.agentscope_runtime.app import ASRuntime
from open_deep_research.agentscope_runtime.production import (
    ProductionRunFactory,
    authorize_run_owner,
)
from open_deep_research.agentscope_runtime.production_resources import (
    production_resources,
)
from open_deep_research.agentscope_runtime.recovery import RecoverySession
from open_deep_research.agentscope_runtime.recovery_store import RunLease
from open_deep_research.agentscope_runtime.run_config import RunConfig


async def execute(request):
    lease = RunLease(**request["lease"])
    task_id = request["task_id"]
    async with AsyncExitStack() as stack:
        from dataclasses import replace
        from uuid import uuid4

        from open_deep_research.agentscope_runtime.settings import ASRuntimeSettings

        settings = ASRuntimeSettings.from_env()
        settings = replace(
            settings,
            rocketmq_group=f"{settings.rocketmq_group}-worker-{uuid4().hex[:12]}",
        )
        runtime = await ASRuntime.create(settings)
        stack.push_async_callback(runtime.aclose)
        if runtime.settings.is_demo:
            raise ValueError("production team executor requires PostgreSQL")
        store = await runtime.create_recovery_store()
        # Borrow the leader epoch; never acquire, renew or release its run lease.
        async with store.transaction(lease):
            pass
        state, _ = await store.load(lease.run_id, lease.user_id)
        recovery = RecoverySession(store, lease, state, model_accounting="gateway")
        root = Path(os.environ.get("RUNS_DIR", ".runs"))
        factory = ProductionRunFactory(
            runtime,
            authorize_run_owner,
            production_resources(root, worker_task_id=task_id),
            runs_dir=root,
            worker_only=True,
        )
        config = RunConfig.restore(state.application["configuration"])
        async with factory(state, config, recovery) as pipeline:
            if pipeline.team_workers is None:
                raise ValueError(
                    "team executor requires a persisted async research run"
                )
            execution = asyncio.create_task(pipeline.team_workers.execute(task_id))
            async def watch_leader():
                while not execution.done():
                    await asyncio.sleep(5)
                    async with store.transaction(lease):
                        pass
            watcher = asyncio.create_task(watch_leader())
            try:
                done, _ = await asyncio.wait({execution, watcher}, return_when=asyncio.FIRST_COMPLETED)
                if watcher in done:
                    await watcher
                return await execution
            finally:
                for task in (execution, watcher):
                    task.cancel()
                await asyncio.gather(execution, watcher, return_exceptions=True)


def main():
    asyncio.run(execute(json.loads(os.environ["AS_TEAM_EXECUTION"])))


if __name__ == "__main__":
    main()
