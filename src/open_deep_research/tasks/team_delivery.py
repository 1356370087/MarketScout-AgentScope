"""Transactional outbox and stable RocketMQ consumers for complete team messages."""

import asyncio
import hashlib
import logging
from uuid import uuid4

from pydantic import ValidationError

from open_deep_research.tasks.team_protocol import TeamEvent
from open_deep_research.tasks.team_store import TeamStore

logger = logging.getLogger(__name__)


class TeamDelivery:
    def __init__(self, pool, settings):
        self.store, self.settings = TeamStore(pool), settings
        self.producer = None
        self.consumers = []
        self.runner = None
        self.closed = False

    def topic(self, control):
        return f"{self.settings.rocketmq_topic_prefix}_team_{'control' if control else 'events'}_v2"

    async def start(self):
        from rocketmq.v5.client import ClientConfiguration, Credentials
        from rocketmq.v5.producer import Producer
        from open_deep_research.tasks.rocketmq_consumer import ManagedPushConsumer
        from rocketmq.v5.consumer.push.message_listener import (
            ConsumeResult,
            MessageListener,
        )
        from rocketmq.v5.model import FilterExpression

        cfg = self.settings
        client = ClientConfiguration(
            cfg.rocketmq_endpoint,
            Credentials(cfg.rocketmq_access_key, cfg.rocketmq_secret_key),
        )
        loop, host = asyncio.get_running_loop(), self

        class Listener(MessageListener):
            def consume(self, message):
                future = asyncio.run_coroutine_threadsafe(
                    host.receive(bytes(message.body)), loop
                )
                try:
                    future.result(timeout=10)
                    return ConsumeResult.SUCCESS
                except Exception:
                    logger.exception("Team message durable receipt failed")
                    return ConsumeResult.FAILURE

        topics = [self.topic(False), self.topic(True)]
        self.producer = Producer(client, topics=topics, tls_enable=cfg.rocketmq_tls)
        await asyncio.to_thread(self.producer.startup)
        try:
            for topic in topics:
                consumer = ManagedPushConsumer(
                    client,
                    consumer_group=topic + "_delivery",
                    message_listener=Listener(),
                    subscription={topic: FilterExpression("*")},
                    tls_enable=cfg.rocketmq_tls,
                    consumption_thread_count=2,
                )
                self.consumers.append(consumer)
                await asyncio.to_thread(consumer.startup)
            self.runner = asyncio.create_task(self.serve())
        except BaseException:
            await self.aclose()
            raise

    async def receive(self, body):
        try:
            if len(body) > 65536:
                raise ValueError("message_too_large")
            event = TeamEvent.model_validate_json(body)
            async with self.store.pool.acquire() as db:
                original = await db.fetchval(
                    "SELECT event FROM research_coordination_events WHERE event_id=$1 AND run_id=$2",
                    event.event_id,
                    event.run_id,
                )
            if original is None or TeamEvent.model_validate_json(original) != event:
                raise ValueError("message_not_committed_by_coordinator")
        except (ValidationError, ValueError) as exc:
            async with self.store.pool.acquire() as db:
                await db.execute(
                    "INSERT INTO research_coordination_rejections(message_key,reason) VALUES($1,$2) ON CONFLICT DO NOTHING",
                    hashlib.sha256(body).hexdigest(),
                    type(exc).__name__,
                )
            logger.warning("Rejected invalid team message: %s", type(exc).__name__)
            return
        await self.store.receive(event)

    async def publish(self, event):
        from rocketmq.v5.model import Message

        message = Message()
        message.topic = self.topic(event.is_control)
        message.body = event.model_dump_json().encode()
        message.keys = event.event_id
        await asyncio.to_thread(self.producer.send, message)

    async def once(self):
        token = uuid4().hex
        async with self.store.pool.acquire() as db:
            rows = await db.fetch(
                """UPDATE research_coordination_outbox o SET lease_token=$1,
                   lease_expires=clock_timestamp()+interval '30 seconds',attempts=attempts+1
                   WHERE event_id IN (SELECT event_id FROM research_coordination_outbox
                     WHERE published_at IS NULL AND next_attempt<=clock_timestamp()
                       AND (lease_expires IS NULL OR lease_expires<clock_timestamp())
                     ORDER BY next_attempt LIMIT 20 FOR UPDATE SKIP LOCKED)
                   RETURNING o.event_id,o.attempts""",
                token,
            )
        for row in rows:
            try:
                async with self.store.pool.acquire() as db:
                    body = await db.fetchval(
                        "SELECT event FROM research_coordination_events WHERE event_id=$1",
                        row["event_id"],
                    )
                await self.publish(TeamEvent.model_validate_json(body))
            except Exception as exc:
                async with self.store.pool.acquire() as db:
                    await db.execute(
                        """UPDATE research_coordination_outbox SET delivery_error=$3,lease_expires=NULL,
                           next_attempt=clock_timestamp()+make_interval(secs=>$4)
                           WHERE event_id=$1 AND lease_token=$2""",
                        row["event_id"],
                        token,
                        type(exc).__name__,
                        min(60, 2 ** min(row["attempts"], 6)),
                    )
            else:
                async with self.store.pool.acquire() as db:
                    await db.execute(
                        """UPDATE research_coordination_outbox SET published_at=clock_timestamp(),delivery_error=NULL,
                           lease_expires=NULL WHERE event_id=$1 AND lease_token=$2""",
                        row["event_id"],
                        token,
                    )

    async def serve(self):
        while not self.closed:
            try:
                await self.once()
            except Exception:
                logger.exception("Team outbox relay retry")
            await asyncio.sleep(0.5)

    async def aclose(self):
        self.closed = True
        if self.runner:
            self.runner.cancel()
            await asyncio.gather(self.runner, return_exceptions=True)
        for target in [*self.consumers, self.producer]:
            if target is not None:
                await asyncio.to_thread(target.shutdown)
        self.consumers.clear()
        self.producer = None
