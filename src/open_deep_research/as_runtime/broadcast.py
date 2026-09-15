"""RocketMQ 跨进程广播桥（M2/AS-T014）。

职责边界（03 目标架构 + AS-D012）：

- 广播只承载**唤醒信号**（瞬态、best-effort）：断线丢失是允许的，
  业务事实由 ``DurableCommandBridge`` 的 PostgreSQL 持久表承载，
  恢复路径从持久表扫描——"广播丢失后可从持久事实恢复"。
- SDK 线程隔离：RocketMQ 5.x 客户端自带线程池，``consume`` 回调经
  ``loop.call_soon_threadsafe`` 注入 asyncio 循环（沿用旧
  ``tasks/rocketmq_transport.py`` 的线程边界模式）。
- ACK 语义：``consume`` 收到信号即返回 SUCCESS——信号不承担持久化责任
  （旧实现 "ACK only durable receipt" 的等价形式：事实在 PG）。

适配 rocketmq-python-client 5.1.2（gRPC 协议，连接 Proxy 端点）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import defaultdict
from typing import Any

logger = logging.getLogger(__name__)


class RocketMQBroadcast:
    """跨进程 publish/subscribe 信号桥；进程内分发 + RocketMQ 5.x gRPC 投递。"""

    def __init__(
        self,
        endpoint: str,
        *,
        topic_prefix: str = "insightforge_as",
        access_key: str = "",
        secret_key: str = "",
        tls: bool = False,
    ) -> None:
        self.endpoint = endpoint
        self.topic_prefix = topic_prefix
        self.access_key = access_key
        self.secret_key = secret_key
        self.tls = tls
        self._producer: Any = None
        self._consumer: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._origin = uuid.uuid4().hex
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)

    def _topic(self, channel: str) -> str:
        return f"{self.topic_prefix}_broadcast_{channel}_v1"

    def _client_config(self) -> Any:
        from rocketmq.v5.client import ClientConfiguration, Credentials

        return ClientConfiguration(
            self.endpoint, Credentials(self.access_key, self.secret_key)
        )

    async def _run_in_executor(self, fn: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(None, fn)

    async def start(self) -> None:
        """初始化 producer（跨进程投递；consumer 按需另行启动）。"""
        from rocketmq.v5.producer import Producer

        self._loop = asyncio.get_running_loop()
        self._producer = Producer(
            self._client_config(),
            topics=[self._topic("wake")],
            tls_enable=self.tls,
        )
        await self._run_in_executor(self._producer.startup)

    async def start_consumer(self, group: str, *, channels: list[str]) -> None:
        """订阅各频道跨进程信号，注入进程内分发器后立即 ACK。"""
        from rocketmq.v5.consumer.push import PushConsumer
        from rocketmq.v5.consumer.push.message_listener import (
            ConsumeResult,
            MessageListener,
        )
        from rocketmq.v5.model import FilterExpression

        self._loop = asyncio.get_running_loop()
        bus = self

        class _Listener(MessageListener):
            def consume(self, message: Any) -> ConsumeResult:
                try:
                    topic = message.topic or ""
                    channel = topic.rsplit("_broadcast_", 1)[-1].removesuffix("_v1")
                    body = (
                        message.body
                        if isinstance(message.body, (bytes, bytearray))
                        else b"{}"
                    )
                    payload = json.loads(bytes(body).decode("utf-8", errors="replace"))
                    if payload.get("origin") == bus._origin:
                        return ConsumeResult.SUCCESS
                    payload = payload["payload"]
                    if bus._loop is not None and bus._loop.is_running():
                        bus._loop.call_soon_threadsafe(bus._dispatch, channel, payload)
                except Exception:  # 信号投递失败可容忍（事实在 PG）
                    logger.debug("broadcast signal dropped", exc_info=True)
                return ConsumeResult.SUCCESS

        subscription = {self._topic(ch): FilterExpression("*") for ch in channels}
        consumer = PushConsumer(
            self._client_config(),
            consumer_group=group,
            message_listener=_Listener(),
            subscription=subscription,
            tls_enable=self.tls,
        )
        self._consumer = consumer
        await self._run_in_executor(consumer.startup)

    def _dispatch(self, channel: str, payload: dict) -> None:
        for q in list(self._subscribers.get(channel, [])):
            q.put_nowait(payload)

    async def publish(self, channel: str, payload: dict) -> None:
        """进程内即时分发 + RocketMQ best-effort 跨进程投递（失败仅日志）。"""
        self._dispatch(channel, payload)
        if self._producer is None:
            return
        from rocketmq.v5.model import Message

        msg = Message()
        msg.topic = self._topic(channel)
        msg.body = json.dumps({"origin": self._origin, "payload": payload}).encode()
        try:
            await self._run_in_executor(lambda: self._producer.send(msg))
        except Exception:
            logger.debug("rocketmq publish best-effort failed", exc_info=True)

    def subscribe(self, channel: str, on_ready=None):
        bus = self

        async def gen():
            q: asyncio.Queue = asyncio.Queue()
            bus._subscribers[channel].append(q)
            try:
                if on_ready is not None:
                    on_ready()
                while True:
                    yield await q.get()
            finally:
                lst = bus._subscribers.get(channel, [])
                if q in lst:
                    lst.remove(q)

        return gen()

    async def aclose(self) -> None:
        for target in (self._consumer, self._producer):
            if target is not None:
                try:
                    await self._run_in_executor(target.shutdown)
                except Exception:
                    pass
        self._producer = None
        self._consumer = None
        self._subscribers.clear()
