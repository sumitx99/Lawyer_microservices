"""API tests run on Postgres only, in a throwaway schema (default `lexaior_test`).

  Local: uses TEST_DATABASE_URL, or else DATABASE_URL from .env (your dev Supabase project);
         only the test schema is created, truncated and dropped, never `lexaior`.
  CI:    TEST_DATABASE_URL points at the Postgres service container.

Object storage is an in-memory stand-in for the S3 calls the folder services make; Kafka (outbox)
and the upload cleanup loop are off, and Valkey points at a closed port (progress pings are best effort)."""
import base64
import hashlib
import os
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _dotenv(name: str) -> str | None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    return None


TEST_DB = os.environ.get("TEST_DATABASE_URL") or _dotenv("DATABASE_URL")
TEST_SCHEMA = os.environ.get("TEST_DB_SCHEMA", "lexaior_test")
if not TEST_DB or not TEST_DB.startswith("postgres"):
    raise SystemExit("Set TEST_DATABASE_URL (or DATABASE_URL in .env) to a Postgres database to run the tests.")
if not TEST_SCHEMA.endswith("_test"):
    raise SystemExit(f"Refusing to run tests in schema '{TEST_SCHEMA}': it must end in _test.")

os.environ.update(DATABASE_URL=TEST_DB, DB_SCHEMA=TEST_SCHEMA, ENVIRONMENT="test", AUTH_PROVIDER="dev",
                  STORAGE_PROVIDER="s3", STORAGE_ENDPOINT_URL="http://storage.test", STORAGE_BUCKET="documents",
                  STORAGE_ACCESS_KEY="test-access", STORAGE_SECRET_KEY="test-storage-secret",
                  VALKEY_HOST="127.0.0.1", VALKEY_PORT="1", VALKEY_PASSWORD="test-cache-secret",
                  OUTBOX_ENABLED="false", UPLOAD_CLEANUP_ENABLED="false", RETRIEVAL_WARMUP="false",
                  SESSION_REVERIFY_MINUTES="0", PROXY_SECRET="", TRUST_PROXY_HEADERS="false",
                  APP_URL="http://testserver", DB_POOL_SIZE="5", DB_POOL_PRE_PING="false")

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from pymicroservices.app import main  # noqa: E402
from pymicroservices.app.models import Base as RagBase  # noqa: E402
from pymicroservices.app.security import crypto  # noqa: E402
from pymicroservices.app.utilities.firm_db import FirmBase, SessionLocal, engine  # noqa: E402
from pymicroservices.app.utilities.storage_helper import StoredObject  # noqa: E402
from scripts import seed_dev  # noqa: E402

app = main.create_app()
METADATA = (FirmBase.metadata, RagBase.metadata)
TABLES = ", ".join(f'"{TEST_SCHEMA}"."{t.name}"' for m in METADATA for t in m.sorted_tables)


@pytest.fixture(scope="session", autouse=True)
def test_schema():
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{TEST_SCHEMA}" CASCADE'))
        conn.execute(text(f'CREATE SCHEMA "{TEST_SCHEMA}"'))
    for metadata in METADATA:
        metadata.create_all(engine)
    yield
    with engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{TEST_SCHEMA}" CASCADE'))


@pytest.fixture(autouse=True)
def fresh_db(test_schema):
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    seed_dev.main()
    yield


class StorageStandIn:
    """The S3 calls the folder services make, in memory. Presigned PUTs enforce size, type and
    SHA-256 like SeaweedFS/S3 do; `browser` plays the browser that uses the presigned URLs."""

    BASE = "http://storage.test/documents/"

    def __init__(self):
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.browser = httpx.Client(transport=httpx.MockTransport(self._handle))

    def presign_put(self, key, content_type, byte_count, sha256_hex, expires):
        checksum = base64.b64encode(bytes.fromhex(sha256_hex)).decode()
        url = f"{self.BASE}{quote(key)}?op=put&size={byte_count}"
        return url, {"Content-Type": content_type, "x-amz-checksum-sha256": checksum}

    def presign_get(self, key, file_name, expires):
        return f"{self.BASE}{quote(key)}?op=get&filename={quote(file_name)}"

    def _handle(self, request: httpx.Request) -> httpx.Response:
        key = request.url.path.removeprefix("/documents/")
        if request.method == "PUT" and request.url.params.get("op") == "put":
            body = request.content
            checksum = base64.b64encode(hashlib.sha256(body).digest()).decode()
            if str(len(body)) != request.url.params["size"] or checksum != request.headers.get("x-amz-checksum-sha256"):
                return httpx.Response(400, text="BadDigest")
            self.objects[key] = (body, request.headers["content-type"])
            return httpx.Response(200)
        if request.method == "GET" and request.url.params.get("op") == "get" and key in self.objects:
            return httpx.Response(200, content=self.objects[key][0])
        return httpx.Response(404)

    async def inspect(self, key):
        if key not in self.objects:
            return None
        body, content_type = self.objects[key]
        return StoredObject(len(body), content_type, hashlib.sha256(body).hexdigest())

    async def read_head(self, key, size=8192):
        return self.objects[key][0][:size]

    async def stream_sha256(self, key):
        return hashlib.sha256(self.objects[key][0]).hexdigest()

    async def delete(self, key):
        self.objects.pop(key, None)

    async def ping(self):
        return True

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def file_store(monkeypatch):
    stand_in = StorageStandIn()
    monkeypatch.setattr(main, "Storage", lambda settings: stand_in)
    return stand_in


class Api:
    """Test client for one console session, sending the CSRF header like the frontend."""

    def __init__(self, console: str = "iam"):
        self.console = console
        self.c = TestClient(app)
        self.c.__enter__()

    def _headers(self, extra=None):
        h = {"x-csrf-token": self.c.cookies.get("lx_csrf") or ""}
        if self.console == "admin":
            h["x-console"] = "admin"
        return {**h, **(extra or {})}

    def get(self, path, **kw):
        return self.c.get(path, headers=self._headers(kw.pop("headers", None)), **kw)

    def post(self, path, json=None, **kw):
        return self.c.post(path, json=json, headers=self._headers(kw.pop("headers", None)), **kw)

    def patch(self, path, json=None, **kw):
        return self.c.patch(path, json=json, headers=self._headers(kw.pop("headers", None)), **kw)

    def put(self, path, json=None, **kw):
        return self.c.put(path, json=json, headers=self._headers(kw.pop("headers", None)), **kw)

    def delete(self, path, **kw):
        return self.c.delete(path, headers=self._headers(kw.pop("headers", None)), **kw)

    def login(self, email, password=seed_dev.DEV_PASSWORD):
        r = self.c.post(f"/api/auth/{self.console}/login", json={"email": email, "password": password})
        assert r.status_code == 200, r.text
        body = r.json()
        if body["status"] == "MFA_SETUP_REQUIRED":
            setup = self.c.post(f"/api/auth/{self.console}/mfa/setup", json={"challenge": body["challenge"]}).json()
            body = self.c.post(f"/api/auth/{self.console}/mfa/setup/verify",
                               json={"challenge": setup["challenge"], "code": crypto.totp(setup["secret"])}).json()
        elif body["status"] == "MFA_REQUIRED":
            body = self.c.post(f"/api/auth/{self.console}/mfa/verify",
                               json={"challenge": body["challenge"], "code": totp_for(self.console, email)}).json()
        assert body["status"] == "success", body
        return self


def as_user(email: str) -> Api:
    return Api("iam").login(email)


def as_admin() -> Api:
    return Api("admin").login("admin@morganlaw.test")


def matter_id(name: str) -> str:
    from pymicroservices.app.firm_models import Matter
    with SessionLocal() as db:
        return db.query(Matter).filter_by(name=name).one().id


def user_id(email: str) -> str:
    from pymicroservices.app.firm_models import IamUser
    with SessionLocal() as db:
        return db.query(IamUser).filter_by(email=email).one().id


def totp_for(console: str, email: str) -> str:
    from pymicroservices.app.firm_models import DevIdentity
    with SessionLocal() as db:
        ident = db.query(DevIdentity).filter_by(console=console, email=email).one()
        return crypto.totp(ident.mfa_secret)
