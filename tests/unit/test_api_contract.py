from pymicroservices.app.main import create_app
from pymicroservices.app.utilities.secrets_control import Settings


def test_ids_are_required_in_payloads_not_urls():
    settings = Settings(_env_file=None, postgres_password='test', valkey_password='test',
                        storage_access_key='test', storage_secret_key='test')
    schema = create_app(settings).openapi()
    expected = {'/documents', '/documents/list', '/documents/status', '/documents/events'}
    assert expected.issubset(schema['paths'])
    # The RAG routes; the website's /api routes (sign-in, matters, collections) use path ids.
    assert all('{' not in path for path in schema['paths'] if not path.startswith('/api/'))
    for path in expected:
        operation = schema['paths'][path]['post']
        assert operation['requestBody']['required']
        assert not any(parameter['in'] in {'path', 'query'} for parameter in operation.get('parameters', []))
    identity = schema['components']['schemas']['DocumentIdentityRequest']
    assert set(identity['required']) == {'project_id', 'doc_id'}
