from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from pymicroservices.app.services.retrigger.retrigger_service import RetriggerService
from pymicroservices.app.services.threads.unstructured.thread_service import derive_thread_status, file_state
from pymicroservices.app.utilities.secrets_control import Settings

NOW = datetime.now(timezone.utc)


def row(file_type='pdf', upload='COMPLETED', **stages):
    values = {'id': uuid4(), 'file_type': file_type, 'upload_status': upload}
    for stage in ('standardization', 'markdown', 'image', 'metaclassifier', 'parquet'):
        status = stages.get(stage)
        values |= {f'{stage}_status': status, f'{stage}_error': stages.get(f'{stage}_error'),
                   f'{stage}_updated_at': stages.get(f'{stage}_updated_at', NOW),
                   f'{stage}_lease_expires_at': stages.get(f'{stage}_lease')}
    return SimpleNamespace(**values)


DONE = dict(standardization='COMPLETED', markdown='COMPLETED', image='INAPPLICABLE', metaclassifier='COMPLETED')


def test_file_and_folder_status():
    assert file_state(row(upload='RUNNING')) == 'uploading'
    assert file_state(row(upload='FAILED')) == 'failed'
    assert file_state(row(standardization='WAITING')) == 'processing'
    assert file_state(row(**DONE)) == 'done'
    assert file_state(row(**(DONE | {'markdown': 'FAILED'}))) == 'failed'
    assert file_state(row('csv', parquet='COMPLETED')) == 'done'  # CSV only has the parquet stage
    assert derive_thread_status([row(upload='RUNNING'), row(**DONE)]) == 'UPLOADING'
    assert derive_thread_status([row(standardization='RUNNING'), row(**DONE)]) == 'PROCESSING'
    assert derive_thread_status([row(**DONE), row('csv', parquet='COMPLETED')]) == 'COMPLETED'
    assert derive_thread_status([row(**DONE), row(upload='FAILED')]) == 'PARTIAL'
    assert derive_thread_status([row(upload='FAILED')]) == 'FAILED'


def service():
    settings = Settings(_env_file=None, postgres_password='t', valkey_password='t', storage_access_key='t',
                        storage_secret_key='t', stage_stuck_seconds=900, standardization_stuck_seconds=1800)
    return RetriggerService(None, settings, None)


def test_retrigger_picks_the_earliest_failed_or_stalled_stage():
    choose = service().choose
    failed = row(standardization='COMPLETED', markdown='FAILED', markdown_error='EmbeddingFailure', image='COMPLETED')
    result = choose(failed, None, NOW)
    assert (result.outcome, result.stage, result.reason) == ('retriggered', 'markdown', 'EmbeddingFailure')
    stalled = row(standardization='RUNNING', standardization_lease=NOW - timedelta(seconds=1))
    assert (choose(stalled, None, NOW).outcome, choose(stalled, None, NOW).stage) == ('retriggered', 'standardization')
    live = row(standardization='RUNNING', standardization_lease=NOW + timedelta(minutes=1))
    assert choose(live, None, NOW).outcome == 'running'


def test_retrigger_waiting_thresholds_and_refusals():
    choose = service().choose
    fresh = row(standardization='WAITING')
    assert choose(fresh, None, NOW).outcome == 'not_retriggerable'
    old_markdown = row(standardization='COMPLETED', markdown='WAITING', markdown_updated_at=NOW - timedelta(minutes=16))
    assert choose(old_markdown, None, NOW).stage == 'markdown'
    # Standardization gets the longer (30 min) threshold for large PDFs.
    waiting_20 = row(standardization='WAITING', standardization_updated_at=NOW - timedelta(minutes=20))
    assert choose(waiting_20, None, NOW).outcome == 'not_retriggerable'
    assert choose(row(upload='FAILED'), None, NOW).outcome == 'not_retriggerable'
    assert choose(row(), 'parquet', NOW).reason == 'parquet does not apply to pdf files.'
    assert choose(row(**DONE), 'standardization', NOW).outcome == 'not_retriggerable'


def test_retrigger_refuses_when_a_dependent_stage_is_running():
    busy = row(standardization='COMPLETED', markdown='COMPLETED', image='FAILED',
               metaclassifier='RUNNING', metaclassifier_lease=NOW + timedelta(minutes=1))
    result = service().choose(busy, None, NOW)
    assert (result.outcome, result.stage) == ('running', 'metaclassifier')


# ---- Project request contracts --------------------------------------------------------------

import pytest
from pydantic import ValidationError
from pymicroservices.app.main import create_app
from pymicroservices.app.schemas import (ProjectCreateRequest, ProjectIdentityRequest, ProjectListRequest,
                                        ProjectThreadsRequest, ProjectUpdateRequest)


@pytest.mark.parametrize('model,payload', [
    (ProjectCreateRequest, {'name': ''}), (ProjectCreateRequest, {'name': ' ' * 3}),
    (ProjectCreateRequest, {'name': 'x' * 256}), (ProjectCreateRequest, {'name': None}),
    (ProjectCreateRequest, {'name': 'Matter', 'extra': True}),
    (ProjectListRequest, {'project_id': str(uuid4())}),
    (ProjectIdentityRequest, {'project_id': 'not-a-uuid'}),
    (ProjectIdentityRequest, {'project_id': str(uuid4()), 'extra': True}),
    (ProjectThreadsRequest, {'project_id': str(uuid4()), 'thread_ids': []}),
    (ProjectThreadsRequest, {'project_id': str(uuid4()), 'thread_ids': [str(uuid4()) for _ in range(101)]}),
    (ProjectThreadsRequest, {'project_id': str(uuid4()), 'thread_ids': [str(uuid4())], 'extra': True}),
    (ProjectUpdateRequest, {'project_id': str(uuid4()), 'name': ''}),
    (ProjectUpdateRequest, {'project_id': str(uuid4()), 'name': '  '}),
    (ProjectUpdateRequest, {'project_id': str(uuid4()), 'name': 'x' * 256}),
    (ProjectUpdateRequest, {'project_id': str(uuid4()), 'name': None}),
    (ProjectUpdateRequest, {'project_id': str(uuid4()), 'extra': True})])
def test_project_requests_reject_invalid_input(model, payload):
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_project_request_boundaries_and_partial_updates():
    assert ProjectCreateRequest(name='x' * 255).name == 'x' * 255
    assert ProjectCreateRequest(name=' Matter ').name == 'Matter'
    assert len(ProjectThreadsRequest(project_id=uuid4(), thread_ids=[uuid4() for _ in range(100)]).thread_ids) == 100
    untouched = ProjectUpdateRequest(project_id=uuid4())
    cleared = ProjectUpdateRequest(project_id=uuid4(), description=None)
    assert untouched.model_fields_set == {'project_id'}
    assert cleared.model_fields_set == {'project_id', 'description'}


def test_all_project_endpoints_use_post_and_require_user_header():
    schema = create_app(service().settings).openapi()
    for endpoint in ('create', 'list', 'get', 'update', 'attach', 'detach', 'delete'):
        path = schema['paths'][f'/projects/{endpoint}']
        assert set(path) == {'post'}
        assert path['post']['requestBody']['required']
        [header] = path['post']['parameters']
        assert header['in'] == 'header' and header['name'] == 'X-User-Id' and header['required']
