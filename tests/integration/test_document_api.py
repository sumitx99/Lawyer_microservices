import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import os
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from botocore.exceptions import EndpointConnectionError
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import OperationalError

from pymicroservices.app.main import create_app
from pymicroservices.app.models import Document, OutboxEvent
from pymicroservices.app.services.rag.outbox_service import OutboxPublisher
from pymicroservices.app.utilities.consumer_helper import EventProducer, create_consumer
from pymicroservices.app.utilities.events import RagEvent
from pymicroservices.app.utilities.secrets_control import get_settings

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get('RAG_RUN_INTEGRATION') != '1', reason='Requires running local infrastructure.')]


@pytest.fixture
async def api():
    settings = get_settings().model_copy(update={'outbox_enabled': False, 'max_upload_bytes': 1024 * 1024})
    app = create_app(settings)
    project = uuid4()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            try:
                yield app, client, project
            finally:
                async with app.state.db.transaction() as session:
                    keys = list(await session.scalars(select(Document.object_key).where(Document.project_id == project)))
                    await session.execute(delete(OutboxEvent).where(OutboxEvent.project_id == project))
                    await session.execute(delete(Document).where(Document.project_id == project))
                for key in keys:
                    await app.state.storage.delete(key)


async def upload(client, project, data, filename='sample.pdf', key=None):
    return await client.post('/documents', data={'project_id': str(project)},
                             files={'file': (filename, data)},
                             headers={'Idempotency-Key': key} if key else {})


async def test_upload_list_status_isolation_and_retry(api, pdf_bytes, docx_bytes):
    app, client, project = api
    first = await upload(client, project, pdf_bytes, key='request-1')
    assert first.status_code == 201, first.text
    document = first.json()
    assert document['status'] == 'PENDING'
    assert 'object_key' not in document
    retry = await upload(client, project, pdf_bytes, key='request-1')
    assert retry.status_code == 200 and retry.json()['id'] == document['id']
    conflict = await upload(client, project, docx_bytes, 'sample.docx', key='request-1')
    assert conflict.status_code == 409
    assert (await upload(client, project, docx_bytes, 'sample.docx')).status_code == 201
    listed = await client.post('/documents/list', json={'project_id': str(project), 'limit': 1})
    assert listed.status_code == 200 and len(listed.json()['items']) == 1
    status = await client.post('/documents/status', json={'project_id': str(project), 'doc_id': document['id']})
    assert status.json()['id'] == document['id']
    other = uuid4()
    assert (await client.post('/documents/list', json={'project_id': str(other)})).json()['items'] == []
    assert (await client.post('/documents/status', json={'project_id': str(other), 'doc_id': document['id']})).status_code == 404
    assert (await client.post('/documents/events', json={'project_id': str(other), 'doc_id': document['id']})).status_code == 404
    async with app.state.db.transaction() as session:
        row = await session.get(Document, UUID(document['id']))
        assert await app.state.storage.get_bytes(row.object_key) == pdf_bytes
        count = await session.scalar(select(func.count()).select_from(OutboxEvent).where(OutboxEvent.aggregate_id == row.id))
        assert count == 1
    # SSE reconnect always receives the current authoritative DB snapshot.
    for _ in range(2):
        stream = app.state.status.stream(project, UUID(document['id']))
        try:
            snapshot = await anext(stream)
            assert 'event: status' in snapshot and document['id'] in snapshot and 'PENDING' in snapshot
        finally:
            await stream.aclose()


async def test_concurrent_idempotency(api, pdf_bytes):
    app, client, project = api
    responses = await asyncio.gather(*(upload(client, project, pdf_bytes, key='concurrent') for _ in range(3)))
    assert sorted(item.status_code for item in responses) == [200, 200, 201]
    assert len({item.json()['id'] for item in responses}) == 1


async def test_api_validation_and_body_limit(api, pdf_bytes):
    app, client, project = api
    assert (await upload(client, project, b'fake PDF')).status_code == 400
    assert (await upload(client, project, b'executable', 'bad.exe')).status_code == 415
    assert (await upload(client, project, b'x' * (1024 * 1024 + 1))).status_code == 413
    bad = await client.post('/documents', data={'project_id': 'not-a-uuid'}, files={'file': ('sample.pdf', pdf_bytes)})
    assert bad.status_code == 422
    assert (await client.post('/documents/list', json={'project_id': str(project), 'limit': 101})).status_code == 422
    oversized = await client.post('/documents', content=b'x' * (3 * 1024 * 1024), headers={'Content-Type': 'multipart/form-data; boundary=x'})
    assert oversized.status_code == 413
    # IDs in query parameters alone are not accepted.
    assert (await client.post('/documents/status', params={'project_id': str(project), 'doc_id': str(uuid4())})).status_code == 422
    async def chunks():
        yield b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="large.pdf"\r\nContent-Type: application/pdf\r\n\r\n'
        for _ in range(3):
            yield b'x' * (1024 * 1024)
        yield b'\r\n--boundary--\r\n'
    streamed = await client.post('/documents', content=chunks(), headers={
        'Content-Type': 'multipart/form-data; boundary=boundary'})
    assert streamed.status_code == 413


async def test_valkey_outage_does_not_break_upload_or_status_stream(api, pdf_bytes, monkeypatch):
    app, client, project = api
    monkeypatch.setattr(app.state.cache.client, 'publish', AsyncMock(side_effect=ConnectionError('cache down')))
    response = await upload(client, project, pdf_bytes)
    assert response.status_code == 201
    document = response.json()
    async with app.state.db.transaction() as session:
        await session.execute(update(Document).where(Document.id == UUID(document['id'])).values(status='FAILED'))
    def unavailable():
        raise ConnectionError('cache down')
    monkeypatch.setattr(app.state.cache.client, 'pubsub', unavailable)
    stream = await client.post('/documents/events', json={'project_id': str(project), 'doc_id': document['id']})
    assert stream.status_code == 200 and 'event: status' in stream.text and 'FAILED' in stream.text


async def test_storage_and_database_failures_leave_no_event(api, pdf_bytes, monkeypatch):
    app, client, project = api
    original_put = app.state.storage.put_bytes
    monkeypatch.setattr(app.state.storage, 'put_bytes', AsyncMock(side_effect=EndpointConnectionError(endpoint_url='http://unavailable')))
    assert (await upload(client, project, pdf_bytes)).status_code == 503
    monkeypatch.setattr(app.state.storage, 'put_bytes', original_put)
    original_transaction = app.state.db.transaction
    calls, written = 0, []
    async def track_put(key, *args):
        written.append(key)
        return await original_put(key, *args)
    @asynccontextmanager
    async def fail_first_transaction():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OperationalError('unavailable', None, Exception('test failure'))
        async with original_transaction() as session:
            yield session
    monkeypatch.setattr(app.state.storage, 'put_bytes', track_put)
    monkeypatch.setattr(app.state.db, 'transaction', fail_first_transaction)
    assert (await upload(client, project, pdf_bytes)).status_code == 503
    monkeypatch.setattr(app.state.db, 'transaction', original_transaction)
    for key in written:
        from botocore.exceptions import ClientError
        with pytest.raises(ClientError):
            await app.state.storage.get_bytes(key)
    async with original_transaction() as session:
        assert await session.scalar(select(func.count()).select_from(Document).where(Document.project_id == project)) == 0
        assert await session.scalar(select(func.count()).select_from(OutboxEvent).where(OutboxEvent.project_id == project)) == 0


async def test_outbox_failure_recovery_and_real_kafka_delivery(api, pdf_bytes):
    app, client, project = api
    response = await upload(client, project, pdf_bytes)
    doc_id = UUID(response.json()['id'])
    async with app.state.db.transaction() as session:
        event_id = await session.scalar(select(OutboxEvent.id).where(OutboxEvent.aggregate_id == doc_id))
    failed = AsyncMock()
    failed.start.side_effect = ConnectionError('simulated Kafka outage')
    publisher = OutboxPublisher(app.state.db, app.state.settings, producer_factory=lambda: failed)
    try:
        assert await publisher.dispatch_one(project_filter=project)
    finally:
        await publisher.close()
    async with app.state.db.transaction() as session:
        row = await session.get(OutboxEvent, event_id)
        assert row.status == 'PENDING' and row.attempt_count == 1
        assert row.available_at > datetime.now(timezone.utc)
        await session.execute(update(OutboxEvent).where(OutboxEvent.id == event_id).values(
            available_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    topic = f'infra.upload.{uuid4().hex}'
    settings = app.state.settings
    admin = AIOKafkaAdminClient(bootstrap_servers=settings.kafka_bootstrap_servers)
    consumer = create_consumer(topic, f'upload-test-{uuid4().hex}', settings)
    def factory():
        producer = EventProducer(settings)
        producer.topics['doc.uploaded'] = topic
        return producer
    publisher = OutboxPublisher(app.state.db, settings, producer_factory=factory)
    try:
        await admin.start()
        await admin.create_topics([NewTopic(topic, 1, 1)])
        await consumer.start()
        assert await publisher.dispatch_one(project_filter=project)
        record = await asyncio.wait_for(consumer.getone(), 20)
        event = RagEvent.model_validate_json(record.value)
        assert event.event_id == event_id and event.doc_id == doc_id and event.project_id == project
        async with app.state.db.transaction() as session:
            row = await session.get(OutboxEvent, event_id)
            assert row.status == 'PUBLISHED' and row.attempt_count == 2 and row.published_at
    finally:
        await consumer.stop()
        await publisher.close()
        await admin.delete_topics([topic])
        await admin.close()


async def test_outbox_leases_prevent_concurrent_delivery_and_recover_expiry(api, pdf_bytes):
    app, client, project = api
    response = await upload(client, project, pdf_bytes)
    doc_id = UUID(response.json()['id'])
    started, release = asyncio.Event(), asyncio.Event()
    sender = AsyncMock()
    async def blocked_send(event):
        started.set()
        await release.wait()
    sender.send.side_effect = blocked_send
    first = OutboxPublisher(app.state.db, app.state.settings, producer_factory=lambda: sender)
    second = OutboxPublisher(app.state.db, app.state.settings, producer_factory=lambda: AsyncMock())
    async with app.state.db.transaction() as session:
        event_id = await session.scalar(select(OutboxEvent.id).where(OutboxEvent.aggregate_id == doc_id))
        await session.execute(update(OutboxEvent).where(OutboxEvent.id == event_id).values(
            lease_owner='previous-worker', lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=60)))
    assert not await first.dispatch_one(project_filter=project)
    async with app.state.db.transaction() as session:
        await session.execute(update(OutboxEvent).where(OutboxEvent.id == event_id).values(
            lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    task = asyncio.create_task(first.dispatch_one(project_filter=project))
    try:
        await asyncio.wait_for(started.wait(), 10)
        assert not await second.dispatch_one(project_filter=project)
        await second.finish(event_id, status='FAILED')
        async with app.state.db.transaction() as session:
            row = await session.get(OutboxEvent, event_id)
            assert row.status == 'PENDING' and row.lease_owner == first.owner
    finally:
        release.set()
        await task
        await first.close()
        await second.close()
    assert sender.send.await_count == 1
    async with app.state.db.transaction() as session:
        row = await session.get(OutboxEvent, event_id)
        assert row.status == 'PUBLISHED' and row.attempt_count == 1
