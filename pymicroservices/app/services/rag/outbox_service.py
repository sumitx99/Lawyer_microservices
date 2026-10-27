"""Leased, at-least-once outbox delivery. Consumers deduplicate by event_id."""
import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import logging
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import or_, select, update

from ...models import OutboxEvent
from ...utilities.consumer_helper import EventProducer
from ...utilities.events import THREAD_EVENTS, RagEvent, ThreadMessage

logger = logging.getLogger(__name__)


class OutboxPublisher:
    def __init__(self, db, settings, producer_factory=None):
        self.db, self.settings = db, settings
        self.owner = str(uuid4())
        self.producer_factory = producer_factory or (lambda: EventProducer(settings))
        self.producer = None

    async def close(self):
        if self.producer is not None:
            producer, self.producer = self.producer, None
            with suppress(Exception):
                await asyncio.wait_for(producer.close(), timeout=5)

    async def dispatch_one(self, project_filter=None):
        now = datetime.now(timezone.utc)
        async with self.db.transaction() as session:
            statement = select(OutboxEvent).where(
                OutboxEvent.status == 'PENDING', OutboxEvent.available_at <= now,
                or_(OutboxEvent.lease_expires_at.is_(None), OutboxEvent.lease_expires_at <= now))
            if project_filter is not None:
                statement = statement.where(OutboxEvent.project_id == project_filter)
            row = await session.scalar(statement.order_by(OutboxEvent.created_at, OutboxEvent.id)
                                       .with_for_update(skip_locked=True).limit(1))
            if row is None:
                return False
            row.lease_owner = self.owner
            row.lease_expires_at = now + timedelta(seconds=60)
            row.attempt_count += 1
            event_id, payload, attempts = row.id, row.payload, row.attempt_count
            project_id, aggregate_id, event_type = row.project_id, row.aggregate_id, row.event_type
        try:
            if event_type in THREAD_EVENTS:
                # Folder messages: aggregate_id is the folder; the payload must name the same folder.
                if ThreadMessage.model_validate(payload).unstructured_thread_id != aggregate_id:
                    raise ValueError('Outbox folder identity mismatch.')
                event = None
            else:
                event = RagEvent.model_validate(payload)
                if (event.event_id, event.project_id, event.doc_id or event.query_id, event.event_type) != (
                        event_id, project_id, aggregate_id, event_type):
                    raise ValueError('Outbox envelope identity mismatch.')
        except (ValidationError, ValueError):
            await self.finish(event_id, status='FAILED', error_message='Invalid event envelope.')
            return True
        try:
            async with asyncio.timeout(20):
                if self.producer is None:
                    self.producer = self.producer_factory()
                    await self.producer.start()
                if event is None:
                    await self.producer.send_thread(event_type, aggregate_id)
                else:
                    await self.producer.send(event)
        except Exception as error:
            await self.close()
            await self.finish(event_id, status='PENDING', error_message=type(error).__name__,
                              available_at=datetime.now(timezone.utc) + timedelta(seconds=min(300, 2 ** min(attempts, 8))))
        else:
            await self.finish(event_id, status='PUBLISHED', error_message=None,
                              published_at=datetime.now(timezone.utc))
        return True

    async def finish(self, event_id, **values):
        async with self.db.transaction() as session:
            await session.execute(update(OutboxEvent).where(
                OutboxEvent.id == event_id, OutboxEvent.lease_owner == self.owner,
                OutboxEvent.status == 'PENDING').values(lease_owner=None, lease_expires_at=None, **values))

    async def run(self):
        try:
            while True:
                try:
                    if await self.dispatch_one():
                        continue
                except Exception as error:
                    logger.warning('Outbox iteration will retry (%s)', type(error).__name__)
                await asyncio.sleep(self.settings.outbox_poll_seconds)
        finally:
            await self.close()
