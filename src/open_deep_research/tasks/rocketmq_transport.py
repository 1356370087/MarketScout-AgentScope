"""RocketMQ 5.x transaction producer and durable push-consumer bridge."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from open_deep_research.tasks.team_protocol import TeamEvent
from open_deep_research.tasks.team_store import TeamStore

logger = logging.getLogger(__name__)


class RocketMQTransport:
    """Keep SDK threads outside the asyncio loop and ACK only durable receipt."""

    def __init__(
        self, store: TeamStore, *, endpoint: str, prefix: str = "insightforge",
        tls: bool = False, access_key: str = "", secret_key: str = "",
        consumer_group_prefix: str | None = None,
    ):
        """Configure a deployment-scoped producer and two independent consumers."""
        self.store = store
        self.endpoint = endpoint
        self.prefix = prefix
        self.consumer_group_prefix = consumer_group_prefix or prefix
        self.tls = tls
        self.access_key = access_key
        self.secret_key = secret_key
        self.producer: Any = None
        self.consumers: list[Any] = []
        self.loop: asyncio.AbstractEventLoop | None = None
        self.wakeups: dict[tuple[str, str], asyncio.Event] = {}
        self.leases: dict[str, Any] = {}
        self.require_lease = False
        self.publishers: dict[str, Any] = {}

    def topic(self, event: TeamEvent) -> str:
        """Select control independently of regular research events."""
        channel = "control" if event.is_control else "events"
        return f"{self.prefix}_coordination_{channel}_v1"

    def signal(self, run_id: str, recipient: str) -> asyncio.Event:
        """Return a lightweight notification; durable input remains in PostgreSQL."""
        return self.wakeups.setdefault((run_id, recipient), asyncio.Event())

    async def start(self) -> None:
        """Register transaction checks before beginning message consumption."""
        from rocketmq.grpc_protocol import TransactionResolution
        from rocketmq.v5.client import ClientConfiguration, Credentials
        from rocketmq.v5.consumer.push.message_listener import (
            ConsumeResult,
            MessageListener,
        )
        from rocketmq.v5.model import FilterExpression
        from rocketmq.v5.producer import Producer, TransactionChecker

        self.loop = asyncio.get_running_loop()
        transport = self

        from open_deep_research.tasks.rocketmq_consumer import ManagedPushConsumer

        class Checker(TransactionChecker):
            def check(self, message):
                try:
                    event = TeamEvent.model_validate_json(message.body)
                    future = asyncio.run_coroutine_threadsafe(
                        transport.store.outcome(event.event_id), transport.loop,
                    )
                    state = future.result(timeout=10)
                    return {
                        "COMMITTED": TransactionResolution.COMMIT,
                        "ABORTED": TransactionResolution.ROLLBACK,
                    }.get(state, TransactionResolution.TRANSACTION_RESOLUTION_UNSPECIFIED)
                except Exception:
                    logger.exception("RocketMQ transaction outcome unavailable")
                    return TransactionResolution.TRANSACTION_RESOLUTION_UNSPECIFIED

        class Listener(MessageListener):
            def consume(self, message):
                try:
                    event = TeamEvent.model_validate_json(message.body)
                    future = asyncio.run_coroutine_threadsafe(
                        transport._receive(event), transport.loop,
                    )
                    future.result(timeout=10)
                    return ConsumeResult.SUCCESS
                except Exception:
                    logger.exception("RocketMQ durable receipt failed")
                    return ConsumeResult.FAILURE

        credentials = Credentials(self.access_key, self.secret_key)
        configuration = ClientConfiguration(self.endpoint, credentials)
        topics = [f"{self.prefix}_coordination_{channel}_v1" for channel in ("events", "control")]
        self.producer = Producer(configuration, topics=topics, checker=Checker(), tls_enable=self.tls)
        try:
            await asyncio.to_thread(self.producer.startup)
            for channel, topic in zip(("events", "control"), topics):
                consumer = ManagedPushConsumer(
                    configuration, f"{self.consumer_group_prefix}_coordination_{channel}_v1",
                    message_listener=Listener(), subscription={topic: FilterExpression("*")},
                    consumption_thread_count=2, tls_enable=self.tls,
                )
                self.consumers.append(consumer)
                await asyncio.to_thread(consumer.startup)
        except BaseException:
            await self.close()
            raise

    async def _receive(self, event: TeamEvent) -> None:
        await self.store.receive(event)
        for recipient in event.recipients:
            self.signal(event.run_id, recipient).set()
        publisher = self.publishers.get(event.run_id)
        if publisher is not None:
            await publisher.publish("research.team.updated", stage="researching",
                payload={"event_id": event.event_id, "event_type": event.type},
                dedupe_key=f"team:{event.event_id}")

    async def transact(self, event: TeamEvent, mutate: Callable[..., Awaitable[Any]]) -> Any:
        """Publish a half message, commit locally, then expose the message."""
        from rocketmq.v5.model import Message

        state, result = await self.store.prepare(event)
        if state == "COMMITTED":
            return result
        if state == "ABORTED":
            raise RuntimeError("coordination_operation_aborted")
        transaction = self.producer.begin_transaction()
        message = Message()
        message.topic = self.topic(event)
        message.body = event.model_dump_json().encode()
        message.keys = event.event_id
        await asyncio.to_thread(self.producer.send, message, transaction)
        try:
            lease = self.leases.get(event.run_id)
            if lease is None and self.require_lease:
                raise RuntimeError("coordination_requires_live_run_lease")
            if lease is None:
                result = await self.store.commit(event, mutate)
            else:
                # Only the short database commit holds the existing file lease;
                # publishing and Broker acknowledgement never hold that lock.
                loop = asyncio.get_running_loop()
                def commit_locked():
                    return asyncio.run_coroutine_threadsafe(
                        asyncio.wait_for(self.store.commit(event, mutate), timeout=8), loop,
                    ).result()
                result = await lease.run_fenced(event.fence_token, commit_locked)
        except BaseException:
            # A disconnected COMMIT may already have succeeded. Resolve it from
            # PostgreSQL instead of telling the broker to roll it back blindly.
            try:
                await self.store.abort(event)
            except Exception:
                logger.exception("Transaction outcome deferred to broker checker")
            raise
        try:
            await asyncio.to_thread(transaction.commit)
        except Exception:
            logger.warning("Broker commit deferred to transaction checker", exc_info=True)
        return result

    async def close(self) -> None:
        """Stop listener threads before releasing their database dependencies."""
        for consumer in self.consumers:
            try:
                await asyncio.to_thread(consumer.shutdown)
            except Exception:
                logger.exception("RocketMQ consumer shutdown failed")
        self.consumers.clear()
        if self.producer is not None:
            await asyncio.to_thread(self.producer.shutdown)
            self.producer = None
        self.leases.clear()
        self.publishers.clear()
        self.wakeups.clear()
