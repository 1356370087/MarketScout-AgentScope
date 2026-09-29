"""Optional HMAC-authenticated callback listener for standalone Gateway evals."""

import asyncio
import socket
from contextlib import asynccontextmanager


@asynccontextmanager
async def gateway_callbacks(service, *, host, port):
    """Own the listener in the same process as its native operation authority."""
    import uvicorn
    from fastapi import FastAPI

    from open_deep_research.configuration import Configuration
    from open_deep_research.sandbox.internal_api import build_internal_sandbox_router

    app = FastAPI()
    app.include_router(
        build_internal_sandbox_router(
            lambda run_id: None,
            native_ledger=service.pipeline_factory.gateway_ledger,
            native_root_key=lambda: (
                Configuration.from_runnable_config(None).sandbox_root_signing_key
            ),
        )
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="error", access_log=False)
    )
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((host, port))
        listener.listen(128)
    except BaseException:
        listener.close()
        raise
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("evaluation_callback_start_failed")
                await asyncio.sleep(0.05)
        yield
    finally:
        server.should_exit = True
        try:
            async with asyncio.timeout(10):
                await task
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:
            listener.close()
