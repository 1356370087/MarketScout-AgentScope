"""真实 RocketMQ 双实例验证；显式配置测试 Proxy 和预建的 topic/group。

AS_TEST_ROCKETMQ=1，AS_TEST_ROCKETMQ_ENDPOINT=172.22.121.109:8081。
AS_TEST_ROCKETMQ_PREFIX 默认为 as_m2_acceptance；预建该前缀的
_broadcast_wake_v1 / _broadcast_control_v1，以及 as-m2-acceptance-reader 消费组。
不默认连接共享部署，不使用发布者本地回声作为成功证据。
"""

import asyncio
import os
import pytest
from open_deep_research.agentscope_runtime.broadcast import RocketMQBroadcast
from open_deep_research.agentscope_runtime.pgbus import PostgreSQLMessageBus

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AS_TEST_ROCKETMQ") != "1",
        reason="需要显式启用专用 RocketMQ 测试端点",
    ),
]


async def test_broadcast_roundtrip_across_rocketmq():
    endpoint = os.environ["AS_TEST_ROCKETMQ_ENDPOINT"]
    prefix = os.environ.get("AS_TEST_ROCKETMQ_PREFIX", "as_m2_acceptance")
    writer = RocketMQBroadcast(endpoint, topic_prefix=prefix)
    reader = RocketMQBroadcast(endpoint, topic_prefix=prefix)
    ready = asyncio.Event()
    writer_bus = PostgreSQLMessageBus("unused", broadcast=writer)
    reader_bus = PostgreSQLMessageBus("unused", broadcast=reader)
    stream = reader_bus.subscribe("session:probe", on_ready=ready.set)
    task = None
    try:
        await writer.start()
        await reader.start_consumer(
            os.environ.get("AS_TEST_ROCKETMQ_GROUP", "as-m2-acceptance-reader"),
            channels=["wake"],
        )
        task = asyncio.create_task(anext(stream))
        await ready.wait()
        await writer_bus.publish("session:probe", {"signal": "hello"})
        assert await asyncio.wait_for(task, 30) == {"signal": "hello"}
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await stream.aclose()
        await reader.aclose()
        await writer.aclose()
