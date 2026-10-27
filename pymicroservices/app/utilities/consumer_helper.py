"""Kafka transport primitives. Durable outbox, retry and DLQ orchestration follow later."""

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.structs import OffsetAndMetadata, TopicPartition

import json

from .events import THREAD_EVENTS, RagEvent
from .secrets_control import Settings, get_settings


class EventProducer:
    def __init__(self, settings: Settings | None = None):
        settings = settings or get_settings()
        self.topics = {'doc.uploaded': settings.kafka_topic_uploaded,
                       'doc.parsed': settings.kafka_topic_parsed,
                       'query.requested': settings.kafka_topic_query}
        self.thread_topics = {event_type: getattr(settings, attribute) for event_type, attribute in THREAD_EVENTS.items()}
        self.client = AIOKafkaProducer(bootstrap_servers=settings.kafka_bootstrap_servers,
                                      enable_idempotence=True, acks='all', request_timeout_ms=15000)

    async def start(self):
        await self.client.start()

    async def send(self, event: RagEvent):
        identity = event.doc_id or event.query_id
        # Same document/query key preserves ordering within a partition.
        key = f'{event.project_id}:{identity}'.encode()
        return await self.client.send_and_wait(self.topics[event.event_type],
                                              event.model_dump_json().encode(), key=key)

    async def send_thread(self, event_type, thread_id):
        """One folder message; keyed by folder so its messages stay ordered on one partition."""
        payload = json.dumps({'unstructured_thread_id': str(thread_id)}).encode()
        return await self.client.send_and_wait(self.thread_topics[event_type], payload, key=str(thread_id).encode())

    async def close(self):
        await self.client.stop()


def create_consumer(topic, group_id, settings: Settings | None = None):
    settings = settings or get_settings()
    return AIOKafkaConsumer(topic, bootstrap_servers=settings.kafka_bootstrap_servers,
                           group_id=group_id, enable_auto_commit=False,
                           auto_offset_reset='earliest', max_poll_records=1,
                           max_poll_interval_ms=900000, request_timeout_ms=30000)


async def commit_record(consumer, record):
    """Call only after durable effects. Process records sequentially per partition."""
    partition = TopicPartition(record.topic, record.partition)
    await consumer.commit({partition: OffsetAndMetadata(record.offset + 1, '')})
