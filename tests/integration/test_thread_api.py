"""Folder pipeline API against real SeaweedFS, Postgres and Kafka, driven the way a browser would."""
import asyncio
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from uuid import UUID, uuid4

from aiokafka.admin import AIOKafkaAdminClient, NewTopic
import httpx
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import delete, select, text

from pymicroservices.app.main import create_app
from pymicroservices.app.models import OutboxEvent, UnstructuredData, UnstructuredThread
from pymicroservices.app.services.rag.outbox_service import OutboxPublisher
from pymicroservices.app.utilities.consumer_helper import create_consumer
from pymicroservices.app.utilities.secrets_control import get_settings

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get('RAG_RUN_INTEGRATION') != '1', reason='Requires running local infrastructure.')]

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 64


@pytest.fixture
async def api():
    settings = get_settings().model_copy(update={'outbox_enabled': False, 'upload_cleanup_enabled': False})
    app = create_app(settings)
    user = 'test-' + uuid4().hex
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test',
                               headers={'X-User-Id': user}) as client:
            try:
                yield app, client, user
            finally:
                async with app.state.db.transaction() as session:
                    threads = list(await session.scalars(select(UnstructuredThread.id).where(UnstructuredThread.created_by == user)))
                    keys = list(await session.scalars(select(UnstructuredData.s3_path).where(UnstructuredData.thread_id.in_(threads))))
                    await session.execute(delete(OutboxEvent).where(OutboxEvent.aggregate_id.in_(threads)))
                    await session.execute(delete(UnstructuredThread).where(UnstructuredThread.id.in_(threads)))
                for key in keys:
                    await app.state.storage.delete(key)


def declared(name, data):
    return {'file_name': name, 'byte_count': len(data), 'sha256': sha256(data).hexdigest()}


async def create(client, files, **extra):
    response = await client.post('/threads/unstructured/create', json={'name': 'Matter 42', 'files': files, **extra})
    assert response.status_code == 201, response.text
    return response.json()


async def put(target, data):
    async with httpx.AsyncClient() as browser:
        return (await browser.put(target['upload_url'], content=data, headers=target['headers'])).status_code


async def outbox(app, thread_id):
    async with app.state.db.transaction() as session:
        return list(await session.scalars(select(OutboxEvent).where(OutboxEvent.aggregate_id == UUID(thread_id))
                                          .order_by(OutboxEvent.created_at)))


async def test_presigned_upload_complete_and_one_folder_message(api, pdf_bytes):
    app, client, user = api
    folder = await create(client, [declared('order.pdf', pdf_bytes), declared('exhibit.png', PNG),
                                   declared('order-copy.pdf', pdf_bytes)], tags=['civil'], jurisdiction='IN')
    assert [s['file_name'] for s in folder['skipped']] == ['order-copy.pdf']  # same SHA-256 in one folder
    by_name = {target['file_name']: target for target in folder['files']}
    assert by_name['order.pdf']['headers']['Content-Type'] == 'application/pdf'
    assert all('X-Amz-Signature' in target['upload_url'] for target in folder['files'])
    assert [await put(by_name['order.pdf'], pdf_bytes), await put(by_name['exhibit.png'], PNG)] == [200, 200]

    response = await client.post('/threads/unstructured/complete-upload', json={'thread_id': folder['thread_id']})
    body = response.json()
    assert response.status_code == 200 and body['thread_status'] == 'PROCESSING'
    assert {f['upload_status'] for f in body['files']} == {'COMPLETED'}
    status = (await client.post('/threads/unstructured/status', json={'thread_id': folder['thread_id']})).json()
    assert status['created_by'] == user and status['tags'] == ['civil'] and status['jurisdiction'] == 'IN'
    for file in status['files']:
        assert list(file['stages']) == ['standardization', 'markdown', 'image', 'metaclassifier']
        assert file['stages']['standardization']['status'] == 'WAITING' and file['stages']['markdown']['status'] is None
    [message] = await outbox(app, folder['thread_id'])
    assert (message.event_type, message.payload) == ('thread.standardization', {'unstructured_thread_id': folder['thread_id']})

    # Completing again changes nothing and queues no second message.
    again = (await client.post('/threads/unstructured/complete-upload', json={'thread_id': folder['thread_id']})).json()
    assert again['thread_status'] == 'PROCESSING' and len(await outbox(app, folder['thread_id'])) == 1

    # The outbox publishes exactly {"unstructured_thread_id": X}, keyed by folder, to the stage topic.
    topic = 'test.stndz.' + uuid4().hex
    settings = app.state.settings.model_copy(update={'kafka_topic_standardization': topic})
    admin = AIOKafkaAdminClient(bootstrap_servers=settings.kafka_bootstrap_servers)
    publisher, consumer = OutboxPublisher(app.state.db, settings), create_consumer(topic, 'test-' + uuid4().hex, settings)
    try:
        await admin.start()
        await admin.create_topics([NewTopic(topic, 1, 1)])
        await consumer.start()
        assert await publisher.dispatch_one(project_filter=UUID(folder['thread_id']))
        record = await asyncio.wait_for(consumer.getone(), 20)
    finally:
        await publisher.close()
        await consumer.stop()
        await admin.delete_topics([topic])
        await admin.close()
    assert json.loads(record.value) == {'unstructured_thread_id': folder['thread_id']}
    assert record.key == folder['thread_id'].encode()
    [message] = await outbox(app, folder['thread_id'])
    assert message.status == 'PUBLISHED'


async def test_storage_rejects_tampering_and_complete_upload_rejects_bad_content(api, pdf_bytes):
    app, client, user = api
    renamed_zip = b'PK\x03\x04' + b'\x00' * 40
    folder = await create(client, [declared('order.pdf', pdf_bytes), declared('fake.pdf', renamed_zip),
                                   declared('late.pdf', pdf_bytes + b'\n% late')])
    targets = {target['file_name']: target for target in folder['files']}
    # SeaweedFS enforces the presigned size, type and SHA-256 during the browser upload itself.
    assert await put(targets['order.pdf'], pdf_bytes[:-1] + b'X') == 400  # same size, different content
    assert await put(targets['order.pdf'], pdf_bytes + b'extra') == 403  # different size
    assert await put(targets['order.pdf'], pdf_bytes) == 200
    assert await put(targets['fake.pdf'], renamed_zip) == 200  # hashes match, but it is not a PDF

    body = (await client.post('/threads/unstructured/complete-upload', json={'thread_id': folder['thread_id']})).json()
    results = {f['file_name']: f for f in body['files']}
    assert results['order.pdf']['upload_status'] == 'COMPLETED'
    assert (results['fake.pdf']['upload_status'], results['fake.pdf']['upload_error']) == (
        'FAILED', 'Content does not look like a PDF file.')
    assert results['late.pdf']['upload_status'] == 'RUNNING'  # not uploaded yet: try again later
    assert body['thread_status'] == 'UPLOADING'
    fake_key = f"THREAD_{folder['thread_id']}/pdf/doc_{targets['fake.pdf']['doc_id']}/fake.pdf"
    assert await app.state.storage.inspect(fake_key) is None  # rejected content is deleted

    # An object written without the checksum (not through the presigned URL) is hashed in full.
    late_key = f"THREAD_{folder['thread_id']}/pdf/doc_{targets['late.pdf']['doc_id']}/late.pdf"
    await app.state.storage.put_bytes(late_key, pdf_bytes + b'\n% LATE', 'application/pdf')
    body = (await client.post('/threads/unstructured/complete-upload', json={'thread_id': folder['thread_id'],
                                                                             'doc_ids': [targets['late.pdf']['doc_id']]})).json()
    assert body['files'][0]['upload_error'] == 'SHA-256 of the stored file does not match the declared hash.'
    assert body['thread_status'] == 'PROCESSING'  # order.pdf still has stages to run; PARTIAL needs nothing in progress


async def test_csv_goes_to_the_parquet_topic(api):
    app, client, user = api
    csv = 'party,amount\nA. Sharma,25000\n'.encode()
    folder = await create(client, [declared('claims.csv', csv)])
    assert await put(folder['files'][0], csv) == 200
    await client.post('/threads/unstructured/complete-upload', json={'thread_id': folder['thread_id']})
    status = (await client.post('/threads/unstructured/status', json={'thread_id': folder['thread_id']})).json()
    assert status['files'][0]['stages'] == {'parquet': status['files'][0]['stages']['parquet']}
    assert status['files'][0]['stages']['parquet']['status'] == 'WAITING'
    [message] = await outbox(app, folder['thread_id'])
    assert message.event_type == 'thread.parquet'


async def test_create_validation(api, pdf_bytes):
    app, client, user = api
    url = '/threads/unstructured/create'
    for files, code in [([declared('virus.exe', b'MZ')], 415), ([{'file_name': 'a.pdf', 'byte_count': 51 * 1024 * 1024,
                                                                  'sha256': 'a' * 64}], 413),
                        ([{'file_name': 'a.pdf', 'byte_count': 10, 'sha256': 'nope'}], 400)]:
        assert (await client.post(url, json={'name': 'x', 'files': files})).status_code == code
    assert (await client.post(url, json={'name': 'x', 'files': [declared('a.pdf', pdf_bytes)]},
                              headers={'X-User-Id': ''})).status_code in {400, 422}
    response = await client.post(url, json={'name': 'x', 'data_fabric_id': str(uuid4()), 'files': [declared('a.pdf', pdf_bytes)]})
    assert response.status_code == 404
    unknown = await client.post('/threads/unstructured/status', json={'thread_id': str(uuid4())})
    assert unknown.status_code == 404


async def uploaded_folder(client, pdf_bytes):
    folder = await create(client, [declared('order.pdf', pdf_bytes)])
    assert await put(folder['files'][0], pdf_bytes) == 200
    await client.post('/threads/unstructured/complete-upload', json={'thread_id': folder['thread_id']})
    return folder['thread_id'], folder['files'][0]['doc_id']


async def set_stages(app, doc_id, sql, **params):
    async with app.state.db.transaction() as session:
        await session.execute(text(f'UPDATE unstructured_data SET {sql} WHERE id=:doc'), {'doc': UUID(doc_id), **params})


async def test_retrigger_resets_the_failed_stage_and_refuses_running_ones(api, pdf_bytes):
    app, client, user = api
    thread_id, doc_id = await uploaded_folder(client, pdf_bytes)
    await set_stages(app, doc_id, """standardization_status='COMPLETED', image_status='COMPLETED',
        markdown_status='FAILED', markdown_error='EmbeddingFailure', markdown_attempts=3""")
    url = '/threads/unstructured/retrigger'
    response = await client.post(url, json={'thread_id': thread_id})
    [result] = response.json()['results']
    assert response.status_code == 200 and (result['outcome'], result['stage']) == ('retriggered', 'markdown')
    stages = (await client.post('/threads/unstructured/status', json={'thread_id': thread_id})).json()['files'][0]['stages']
    assert (stages['markdown']['status'], stages['markdown']['attempts'], stages['markdown']['error']) == ('WAITING', 0, None)
    assert stages['image']['status'] == 'COMPLETED' and stages['metaclassifier']['status'] is None
    assert [m.event_type for m in await outbox(app, thread_id)][-1] == 'thread.markdown'

    # A live worker cannot be interrupted: 409, nothing changes.
    live = datetime.now(timezone.utc) + timedelta(minutes=2)
    await set_stages(app, doc_id, "markdown_status='RUNNING', markdown_owner='w1', markdown_lease_expires_at=:lease", lease=live)
    response = await client.post(url, json={'thread_id': thread_id, 'stage': 'markdown'})
    assert response.status_code == 409 and response.json()['results'][0]['outcome'] == 'running'
    # The same worker after it died (lease expired) can be retriggered.
    await set_stages(app, doc_id, "markdown_lease_expires_at=:lease", lease=datetime.now(timezone.utc) - timedelta(seconds=1))
    response = await client.post(url, json={'thread_id': thread_id, 'stage': 'markdown'})
    assert response.json()['results'][0]['outcome'] == 'retriggered'
    assert 'stalled' in response.json()['results'][0]['reason']
    assert (await client.post(url, json={'thread_id': thread_id, 'stage': 'parquet'})).json()['results'][0]['outcome'] == 'not_retriggerable'
    assert (await client.post(url, json={'thread_id': thread_id, 'doc_ids': [str(uuid4())]})).status_code == 404


async def test_abandoned_uploads_fail_and_their_objects_are_removed(api, pdf_bytes):
    app, client, user = api
    folder = await create(client, [declared('never-finished.pdf', pdf_bytes)])
    key = f"THREAD_{folder['thread_id']}/pdf/doc_{folder['files'][0]['doc_id']}/never-finished.pdf"
    await app.state.storage.put_bytes(key, pdf_bytes[:20], 'application/pdf')  # a partial object left behind
    await set_stages(app, folder['files'][0]['doc_id'], "created_at=now()-interval '2 days'")
    assert await app.state.threads.cleanup_abandoned() >= 1
    status = (await client.post('/threads/unstructured/status', json={'thread_id': folder['thread_id']})).json()
    assert status['status'] == 'FAILED' and status['files'][0]['upload_status'] == 'FAILED'
    assert 'not completed within 24 hours' in status['files'][0]['upload_error']
    assert await app.state.storage.inspect(key) is None


async def test_events_stream_sends_the_folder_snapshot(api, pdf_bytes):
    app, client, user = api
    thread_id, _ = await uploaded_folder(client, pdf_bytes)
    # ASGITransport buffers whole responses, so read the open-ended stream from its generator.
    stream = app.state.threads.stream(UUID(thread_id))
    try:
        first = await asyncio.wait_for(anext(stream), 10)
        assert first.startswith('event: status\ndata: ')
        snapshot = json.loads(first.split('data: ', 1)[1])
        assert snapshot['thread_id'] == thread_id and snapshot['status'] == 'PROCESSING'
        await app.state.threads.notify(UUID(thread_id))  # a notification with no change sends nothing new
        await set_stages(app, _, "standardization_status='RUNNING', standardization_owner='w', "
                                  "standardization_lease_expires_at=now()+interval '1 minute'")
        await app.state.threads.notify(UUID(thread_id))
        second = json.loads((await asyncio.wait_for(anext(stream), 10)).split('data: ', 1)[1])
        assert second['files'][0]['stages']['standardization']['status'] == 'RUNNING'
    finally:
        await stream.aclose()
    assert (await client.post('/threads/unstructured/events', json={'thread_id': str(uuid4())})).status_code == 404
