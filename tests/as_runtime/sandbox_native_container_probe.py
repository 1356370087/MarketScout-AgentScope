"""Run native sandbox component probes inside the production dependency image."""

import asyncio
import json
import runpy
import sys

import pytest


async def main():
    provider = runpy.run_path("/probes/test_sandbox_provider.py")
    for name, test in provider.items():
        if name.startswith("test_"):
            await test()
    worker = runpy.run_path("/probes/test_native_sandbox_worker.py")
    with pytest.MonkeyPatch.context() as patch:
        await worker["test_worker_native_model_loop_and_gateway_tool"](patch)
    assert not any(name.startswith("langchain") for name in sys.modules)
    print(
        json.dumps(
            {
                "status": "passed",
                "probe": "native_sandbox_components",
                "langchain_loaded": False,
                "real_external_services": False,
            }
        )
    )


asyncio.run(main())
