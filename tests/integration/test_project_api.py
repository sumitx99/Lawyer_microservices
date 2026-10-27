"""Owner-scoped project APIs against real Postgres, without uploading file objects."""
import asyncio
from hashlib import sha256
import os
from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import delete, func, select, text

from pymicroservices.app.main import create_app
from pymicroservices.app.models import DataFabric, FabricUnstructuredThread, OutboxEvent, UnstructuredData, UnstructuredThread
from pymicroservices.app.utilities.secrets_control import get_settings

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get('RAG_RUN_INTEGRATION') != '1', reason='Requires running local infrastructure.')]


@pytest.fixture
async def api():
    settings = get_settings().model_copy(update={'outbox_enabled': False, 'upload_cleanup_enabled': False})
    app = create_app(settings)
    users = ['project-test-' + uuid4().hex for _ in range(2)]
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test',
                               headers={'X-User-Id': users[0]}) as client:
            try:
                yield app, client, users
            finally:
                async with app.state.db.transaction() as session:
                    threads = select(UnstructuredThread.id).where(UnstructuredThread.created_by.in_(users))
                    await session.execute(delete(OutboxEvent).where(OutboxEvent.aggregate_id.in_(threads)))
                    await session.execute(delete(DataFabric).where(DataFabric.created_by.in_(users)))
                    await session.execute(delete(UnstructuredThread).where(UnstructuredThread.created_by.in_(users)))
                async with app.state.db.transaction() as session:
                    assert not await session.scalar(select(func.count()).select_from(DataFabric)
                        .where(DataFabric.created_by.in_(users)))
                    assert not await session.scalar(select(func.count()).select_from(UnstructuredThread)
                        .where(UnstructuredThread.created_by.in_(users)))


async def post(client, endpoint, payload, code=200, user=None):
    headers = {'X-User-Id': user} if user else {}
    response = await client.post('/projects/' + endpoint, json=payload, headers=headers)
    assert response.status_code == code, response.text
    return response.json()


async def project(client, user=None, **values):
    return await post(client, 'create', {'name': 'Fictional matter', **values}, code=201, user=user)


async def folder(client, user=None, **extra):
    data = b'%PDF-1.7 fictional order'
    response = await client.post('/threads/unstructured/create', json={
        'name': 'Fictional folder', 'files': [{'file_name': 'order.pdf', 'byte_count': len(data),
        'sha256': sha256(data).hexdigest()}], **extra}, headers={'X-User-Id': user} if user else {})
    assert response.status_code == 201, response.text
    return response.json()


async def test_project_lifecycle_counts_and_non_destructive_deletion(api):
    app, client, users = api
    p = await project(client, description='A fictional client matter')
    identity = {'project_id': p['id']}
    assert p['created_by'] == users[0] and p['description'] == 'A fictional client matter'
    folders = [await folder(client), await folder(client)]
    thread_ids = [f['thread_id'] for f in folders]
    doc_ids = [UUID(f['files'][0]['doc_id']) for f in folders]
    async with app.state.db.transaction() as session:
        done = await session.get(UnstructuredData, doc_ids[0])
        done.upload_status = 'COMPLETED'
        done.standardization_status = done.markdown_status = done.metaclassifier_status = 'COMPLETED'
        done.image_status = 'INAPPLICABLE'
        failed = await session.get(UnstructuredData, doc_ids[1])
        failed.upload_status = 'FAILED'
        session.add(UnstructuredData(thread_id=UUID(thread_ids[1]), file_name='pending.txt', file_type='txt', category='text',
            byte_count=10, sha256='a' * 64, s3_path=f'THREAD_{thread_ids[1]}/pending.txt',
            upload_status='COMPLETED', standardization_status='WAITING'))
        session.add(UnstructuredData(thread_id=UUID(thread_ids[1]), file_name='uploading.txt', file_type='txt', category='text',
            byte_count=10, sha256='b' * 64, s3_path=f'THREAD_{thread_ids[1]}/uploading.txt'))
        session.add(UnstructuredData(thread_id=UUID(thread_ids[0]), file_name='table.csv', file_type='csv', category='csv',
            byte_count=10, sha256='c' * 64, s3_path=f'THREAD_{thread_ids[0]}/table.csv',
            upload_status='COMPLETED', parquet_status='INAPPLICABLE'))
    response = await post(client, 'attach', {**identity, 'thread_ids': thread_ids})
    assert response == {**identity, 'attached': thread_ids, 'already_attached': []}
    first = (await post(client, 'list', {}))['items'][0]
    assert (first['folder_count'], first['file_count']) == (2, 5)
    assert first['file_counts'] == {'processing': 2, 'done': 2, 'failed': 1}
    detail = await post(client, 'get', identity)
    by_id = {f['id']: f for f in detail['folders']}
    assert set(by_id) == set(thread_ids)
    assert by_id[thread_ids[0]]['status'] == 'COMPLETED' and by_id[thread_ids[0]]['file_count'] == 2
    assert by_id[thread_ids[1]]['status'] == 'UPLOADING' and by_id[thread_ids[1]]['file_count'] == 3
    assert all(f['attached_at'] for f in detail['folders'])
    updated = await post(client, 'update', {**identity, 'name': 'Renamed matter'})
    assert updated['name'] == 'Renamed matter' and updated['description'] == p['description']
    assert updated['updated_at'] > p['updated_at']
    cleared = await post(client, 'update', {**identity, 'description': None})
    assert cleared['name'] == 'Renamed matter' and cleared['description'] is None
    assert (await post(client, 'detach', {**identity, 'thread_ids': [thread_ids[0]]}))['detached'] == [thread_ids[0]]
    assert (await post(client, 'detach', {**identity, 'thread_ids': [thread_ids[0]]}))['detached'] == []
    assert len((await post(client, 'get', identity))['folders']) == 1
    assert await post(client, 'delete', identity) == {**identity, 'deleted': True}
    assert (await post(client, 'list', {}))['items'] == []
    async with app.state.db.transaction() as session:
        assert await session.scalar(select(func.count()).select_from(UnstructuredThread)
            .where(UnstructuredThread.created_by == users[0])) == 2
        assert await session.scalar(select(func.count()).select_from(UnstructuredData)
            .where(UnstructuredData.thread_id.in_([UUID(i) for i in thread_ids]))) == 5
        assert not await session.scalar(select(func.count()).select_from(FabricUnstructuredThread)
            .where(FabricUnstructuredThread.fabric_id == UUID(p['id'])))


async def test_attach_idempotency_many_to_many_and_newest_order(api):
    app, client, users = api
    first, second = await project(client), await project(client)
    f = await folder(client)
    payload = {'project_id': first['id'], 'thread_ids': [f['thread_id'], f['thread_id']]}
    attached = await post(client, 'attach', payload)
    assert attached['attached'] == [f['thread_id']]
    detail = await post(client, 'get', {'project_id': first['id']})
    attached_at = detail['folders'][0]['attached_at']
    again = await post(client, 'attach', payload)
    assert again['attached'] == [] and again['already_attached'] == [f['thread_id']]
    assert (await post(client, 'get', {'project_id': first['id']}))['folders'][0]['attached_at'] == attached_at
    await post(client, 'attach', {'project_id': second['id'], 'thread_ids': [f['thread_id']]})
    listed = (await post(client, 'list', {}))['items']
    assert [p['id'] for p in listed] == [second['id'], first['id']]
    assert all(p['folder_count'] == p['file_count'] == 1 for p in listed)
    await post(client, 'delete', {'project_id': first['id']})
    assert len((await post(client, 'get', {'project_id': second['id']}))['folders']) == 1


async def test_cross_user_and_unknown_projects_return_identical_not_found(api):
    app, client, users = api
    p = await project(client)
    foreign_folder = await folder(client)
    own_folder = await folder(client, user=users[1])
    assert (await post(client, 'list', {}, user=users[1]))['items'] == []
    for endpoint, extra in [('get', {}), ('update', {'name': 'stolen'}), ('delete', {}),
                            ('attach', {'thread_ids': [own_folder['thread_id']]}),
                            ('detach', {'thread_ids': [foreign_folder['thread_id']]})]:
        foreign = await post(client, endpoint, {'project_id': p['id'], **extra}, code=404, user=users[1])
        missing = await post(client, endpoint, {'project_id': str(uuid4()), **extra}, code=404, user=users[1])
        assert foreign == missing == {'detail': 'Project not found.'}
    owned = await project(client, user=users[1])
    for endpoint in ('attach', 'detach'):
        response = await post(client, endpoint, {'project_id': owned['id'],
            'thread_ids': [own_folder['thread_id'], foreign_folder['thread_id']]}, code=404, user=users[1])
        assert response == {'detail': 'Folder not found.'}
    assert (await post(client, 'get', {'project_id': owned['id']}, user=users[1]))['folders'] == []
    assert (await post(client, 'get', {'project_id': p['id']}))['name'] == p['name']


async def test_folder_creation_checks_project_ownership_and_links_owned_project(api):
    app, client, users = api
    p = await project(client)
    payload = {'name': 'Foreign project', 'data_fabric_id': p['id'], 'files': [
        {'file_name': 'order.pdf', 'byte_count': 10, 'sha256': 'd' * 64}]}
    response = await client.post('/threads/unstructured/create', json=payload, headers={'X-User-Id': users[1]})
    assert response.status_code == 404 and 'not found' in response.json()['detail']
    async with app.state.db.transaction() as session:
        assert not await session.scalar(select(func.count()).select_from(UnstructuredThread)
            .where(UnstructuredThread.created_by == users[1]))
    f = await folder(client, data_fabric_id=p['id'])
    assert [item['id'] for item in (await post(client, 'get', {'project_id': p['id']}))['folders']] == [f['thread_id']]


async def test_project_headers_validation_and_existing_update_trigger(api):
    app, client, users = api
    for endpoint, payload in [('create', {'name': 'Matter'}), ('list', {}), ('get', {'project_id': str(uuid4())}),
                             ('update', {'project_id': str(uuid4())}), ('delete', {'project_id': str(uuid4())}),
                             ('attach', {'project_id': str(uuid4()), 'thread_ids': [str(uuid4())]}),
                             ('detach', {'project_id': str(uuid4()), 'thread_ids': [str(uuid4())]})]:
        request = client.build_request('POST', '/projects/' + endpoint, json=payload)
        del request.headers['X-User-Id']
        assert (await client.send(request)).status_code == 422
        assert (await client.post('/projects/' + endpoint, json=payload, headers={'X-User-Id': '   '})).status_code == 400
    assert (await client.post('/projects/create', json={'name': '', 'extra': True})).status_code == 422
    p = await project(client)
    async with app.state.db.transaction() as session:
        triggers = await session.scalar(text("SELECT count(*) FROM pg_trigger WHERE tgrelid='data_fabric'::regclass "
                                             "AND tgname='touch_updated_at' AND NOT tgisinternal"))
        assert triggers == 1
        updated = await session.scalar(text('UPDATE data_fabric SET name=:name WHERE id=:id AND created_by=:user RETURNING updated_at'),
            {'id': UUID(p['id']), 'name': 'SQL changed', 'user': users[0]})
    assert updated.isoformat() > p['updated_at']


async def test_concurrent_attach_inserts_one_link(api):
    app, client, users = api
    p = await project(client)
    f = await folder(client)
    payload = {'project_id': p['id'], 'thread_ids': [f['thread_id']]}
    responses = await asyncio.gather(post(client, 'attach', payload), post(client, 'attach', payload))
    assert sum(len(r['attached']) for r in responses) == 1
    assert sum(len(r['already_attached']) for r in responses) == 1
    assert len((await post(client, 'get', {'project_id': p['id']}))['folders']) == 1


async def test_project_reads_filter_legacy_cross_owner_links(api):
    app, client, users = api
    p = await project(client)
    foreign = await folder(client, user=users[1])
    async with app.state.db.transaction() as session:
        session.add(FabricUnstructuredThread(fabric_id=UUID(p['id']), thread_id=UUID(foreign['thread_id'])))
    assert (await post(client, 'get', {'project_id': p['id']}))['folders'] == []
    summary = (await post(client, 'list', {}))['items'][0]
    assert summary['folder_count'] == summary['file_count'] == 0
    assert summary['file_counts'] == {'processing': 0, 'done': 0, 'failed': 0}
