"""RocketMQBroadcast 进程内分发路径单测（不依赖 RocketMQ，随常规测试运行）。"""

from __future__ import annotations

import asyncio

import pytest

from open_deep_research.agentscope_runtime.broadcast import RocketMQBroadcast

pytestmark = pytest.mark.asyncio


async def test_inproc_dispatch_and_no_history() -> None:
    bridge = RocketMQBroadcast("unused:8081")  # 不 start → publish 仅进程内
    received: list[dict] = []
    ready = asyncio.Event()
    gen = bridge.subscribe("wake", on_ready=ready.set)
    task = asyncio.create_task(_collect(gen, received))
    await ready.wait()
    await bridge.publish("wake", {"a": 1})
    await bridge.publish("wake", {"a": 2})
    await asyncio.sleep(0.1)
    task.cancel()
    await bridge.publish("wake", {"lost": True})  # 断线后
    await asyncio.sleep(0.1)
    assert [m.get("a") for m in received] == [1, 2]

    after: list[dict] = []
    gen2 = bridge.subscribe("wake")
    t2 = asyncio.create_task(_collect(gen2, after))
    await asyncio.sleep(0.15)
    t2.cancel()
    assert after == [], "无历史补发（信号语义）"


async def test_close_clears_subscribers() -> None:
    bridge = RocketMQBroadcast("unused:8081")
    await bridge.aclose()
    assert bridge._subscribers == {}


async def _collect(gen, sink: list[dict]) -> None:
    try:
        async for item in gen:
            sink.append(item)
    except (asyncio.CancelledError, StopAsyncIteration, GeneratorExit):
        return
