"""Hosted-deployment safety: staging/production settings guards and the proxy secret."""
import pytest
from pydantic import ValidationError

from pymicroservices.app.utilities.secrets_control import Settings, get_settings
from tests.api.conftest import Api

STRONG = "x" * 48
HOSTED = dict(database_url="postgresql://u:p@db.example/postgres", storage_provider="s3",
              storage_access_key="key", storage_secret_key="storage-secret", valkey_password="cache-secret", secret_key=STRONG, cookie_secure=True, admin_require_mfa=True,
              auth_provider="workos", workos_api_key="sk_test_abc", workos_client_id="client_abc", _env_file=None)


def test_staging_accepts_workos_test_key_but_production_does_not():
    assert Settings(environment="staging", **HOSTED).hosted
    with pytest.raises(ValidationError, match="sk_live_"):
        Settings(environment="production", **HOSTED)
    Settings(environment="production", **{**HOSTED, "workos_api_key": "sk_live_abc"})


@pytest.mark.parametrize("override, message", [
    ({"auth_provider": "dev"}, "AUTH_PROVIDER=dev"),
    ({"secret_key": "short"}, "SECRET_KEY"),
    ({"cookie_secure": False}, "COOKIE_SECURE"),
    ({"admin_require_mfa": False}, "ADMIN_REQUIRE_MFA"),
])
def test_staging_refuses_unsafe_settings(override, message):
    """Each unsafe setting alone is refused."""
    with pytest.raises(ValidationError, match=message):
        Settings(environment="staging", **{**HOSTED, **override})


def test_api_requires_the_proxy_secret_when_set(monkeypatch):
    monkeypatch.setattr(get_settings(), "proxy_secret", "proxy-s3cret")
    direct = Api().c
    assert direct.get("/api/auth/config").status_code == 404
    assert direct.get("/api/auth/config", headers={"x-lexaior-proxy": "wrong"}).status_code == 404
    assert direct.get("/api/auth/config", headers={"x-lexaior-proxy": "proxy-s3cret"}).status_code == 200
    assert direct.get("/health").status_code == 200  # Render's health check doesn't go through the proxy


def test_postgres_and_storage_are_required():
    with pytest.raises(ValidationError, match="Postgres"):
        Settings(environment="staging", **{**HOSTED, "database_url": "sqlite:///x.db"})
    with pytest.raises(ValidationError, match="placeholders"):
        Settings(environment="staging", **{**HOSTED, "storage_secret_key": ""})


def test_supabase_url_works_for_both_drivers():
    s = Settings(environment="staging", **{**HOSTED, "database_url": "postgresql://u:p%40ss@db.example:5432/postgres?sslmode=require"})
    assert s.sync_database_url.drivername == "postgresql+psycopg" and s.sync_database_url.query == {"sslmode": "require"}
    assert s.database_url.drivername == "postgresql+asyncpg" and s.database_url.password == "p@ss"
    assert not s.database_url.query and s.database_connect_args == {"ssl": "require"}


def test_x_user_id_routes_are_not_mounted_when_hosted():
    from fastapi.routing import iter_route_contexts

    from pymicroservices.app.main import create_app
    paths = {route.path for route in iter_route_contexts(create_app(Settings(environment="staging", **HOSTED)).routes)}
    assert "/api/collections/{collection_id}/uploads" in paths
    assert not paths & {"/threads/unstructured/create", "/projects/create", "/retrieval/search", "/documents"}
