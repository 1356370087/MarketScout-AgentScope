"""Business-input adapter for the existing Supervisor journal boundary."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from open_deep_research.tasks.mailbox import MailboxMessage
from open_deep_research.tasks.team_protocol import TeamEvent
from open_deep_research.tasks.team_runtime import team_runtime


class TeamInbox:
    """Expose durable input without application-owned delivery leases."""

    def __init__(self, run_id: str):
        """Scope every operation to its run."""
        self.run_id = run_id

    async def send(self, *, recipient, sender, message_type, payload, priority=40, dedupe_key=None):
        """Send a small notification using a transaction outcome record."""
        del priority
        service = await team_runtime.start()
        lease = service.transport.leases.get(self.run_id)
        event = TeamEvent(
            run_id=self.run_id, sender=sender, recipients=[recipient], type=message_type,
            payload=payload, operation_id=dedupe_key or str(uuid4()),
            fence_token=lease.fence_token if lease else 0,
        )

        async def commit(db):
            del db
            return {"event_id": event.event_id}

        await service.transport.transact(event, commit)
        return self._message(event)

    @staticmethod
    def _message(event: TeamEvent) -> MailboxMessage:
        return MailboxMessage(
            message_id=event.event_id, run_id=event.run_id, sender=event.sender,
            recipient=event.recipients[0], type=event.type, payload=event.payload,
            priority=0 if event.is_control else 40,
        )

    async def claim(self, *, agent_id, consumer_id):
        """Read unapplied business input; the broker has already been ACKed."""
        del consumer_id
        from open_deep_research.tasks.teammate_pool import find_active_teammate_pool
        pool = find_active_teammate_pool(self.run_id)
        if pool is not None:
            pool.check_health()
        service = await team_runtime.start()
        events = await service.store.pending(self.run_id, agent_id)
        return sorted((self._message(event) for event in events), key=lambda message: message.priority)

    async def ack(self, *, agent_id, consumer_id, message_ids):
        """Mark business input applied after the caller's durable commit."""
        del consumer_id
        service = await team_runtime.start()
        await service.store.applied(self.run_id, agent_id, message_ids)

    async def stats(self, agent_id):
        """Return pending business-input count."""
        return {"available": len(await self.claim(agent_id=agent_id, consumer_id=""))}

    async def wait_and_claim(self, *, agent_id, consumer_id, timeout_seconds, poll_interval_seconds=0):
        """Wait on listener notification, with no filesystem polling."""
        del poll_interval_seconds
        service = await team_runtime.start()
        signal = service.transport.signal(self.run_id, agent_id)
        signal.clear()
        messages = await self.claim(agent_id=agent_id, consumer_id=consumer_id)
        if messages:
            return messages
        try:
            await asyncio.wait_for(signal.wait(), timeout_seconds)
        except TimeoutError:
            return []
        return await self.claim(agent_id=agent_id, consumer_id=consumer_id)
