"""Validated runtime settings. No clients or network connections are created here.

One Settings object for the whole service: sign-in, sessions and firm rules (the Admin and IAM
consoles) and the RAG pipeline (Postgres, Valkey, Kafka, S3 storage, retrieval models).
"""

from functools import lru_cache
import os
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL, make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra='ignore', case_sensitive=False, hide_input_in_errors=True,
                                      populate_by_name=True)

    # staging = a hosted test deployment: every production safety check except the WorkOS
    # live key, so it can run on WorkOS staging (sk_test_) while roles are being tried out.
    environment: Literal['development', 'test', 'staging', 'production'] = 'development'

    # Database: either one DATABASE_URL (Supabase session pooler, port 5432) or the POSTGRES_* parts.
    database_dsn: str = Field(default='', validation_alias=AliasChoices('database_url', 'database_dsn'))
    postgres_host: str = '127.0.0.1'
    postgres_port: int = Field(default=5433, ge=1, le=65535)
    postgres_db: str = 'lexaior_rag'
    postgres_user: str = 'lexaior'
    postgres_password: SecretStr = SecretStr('')
    # Every table lives in this schema. Kept out of `public` so Supabase's Data API
    # (which exposes `public` to the publishable key) can never reach it.
    db_schema: str = 'lexaior'
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=5, ge=0, le=50)
    db_pool_pre_ping: bool = True  # tests turn it off for speed

    valkey_host: str = '127.0.0.1'
    valkey_port: int = Field(default=6380, ge=1, le=65535)
    valkey_password: SecretStr
    kafka_bootstrap_servers: str = '127.0.0.1:9093'
    kafka_topic_uploaded: str = 'doc.uploaded'
    kafka_topic_parsed: str = 'doc.parsed'
    kafka_topic_query: str = 'query.requested'
    # Folder pipeline topics (design doc names); each carries {"unstructured_thread_id": X}.
    kafka_topic_standardization: str = 'STNDZ_TOPIC'
    kafka_topic_csv_parquet: str = 'CSV_PARQUET_TOPIC'
    kafka_topic_markdown: str = 'RAG_MARKDOWN_TOPIC'
    kafka_topic_image: str = 'RAG_IMAGE_TOPIC'
    kafka_topic_metaclassifier: str = 'METACLASSIFIER_TOPIC'
    # S3-compatible storage: SeaweedFS locally, any S3 endpoint when hosted.
    storage_provider: str = 'seaweedfs'
    storage_endpoint_url: str = 'http://127.0.0.1:8333'
    storage_access_key: SecretStr
    storage_secret_key: SecretStr
    storage_bucket: str = 'rag-documents'
    storage_region: str = 'us-east-1'
    download_url_ttl_seconds: int = Field(default=300, ge=30, le=3600)
    embedding_model: str = 'BAAI/bge-m3'
    embedding_dimension: int = Field(default=1024, ge=1024, le=1024)
    embedding_model_revision: str = '5617a9f61b028005a4858fdac845db406aefb181'
    embedding_max_length: int = Field(default=1024, ge=128, le=8192)
    embedding_cache_dir: str = '../kafka/.cache/huggingface/hub'
    embedding_device: str = Field(default='auto', pattern='^(auto|cpu|mps|cuda)$')
    reranker_enabled: bool = True
    reranker_model: str = 'BAAI/bge-reranker-v2-m3'
    reranker_revision: str = '953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e'
    retrieval_candidates: int = Field(default=50, ge=1, le=200)
    retrieval_rrf_k: int = Field(default=60, ge=1, le=1000)
    retrieval_warmup: bool = False
    llm_base_url: str = 'http://127.0.0.1:11434/v1'
    llm_model: str = 'qwen3:4b'
    llm_api_key: SecretStr = SecretStr('ollama')
    reference_extraction_enabled: bool = False
    max_upload_bytes: int = Field(default=20 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)
    outbox_enabled: bool = True
    outbox_poll_seconds: float = Field(default=1.0, ge=0.1, le=60)
    # Folder uploads: browser PUTs straight to storage with a presigned URL bound to size, type and SHA-256.
    thread_max_files: int = Field(default=100, ge=1, le=1000)
    thread_max_file_bytes: int = Field(default=50 * 1024 * 1024, ge=1024, le=500 * 1024 * 1024)
    presign_expiry_seconds: int = Field(default=900, ge=60, le=86400)
    upload_abandon_seconds: int = Field(default=86400, ge=300, le=7 * 86400)
    upload_cleanup_interval_seconds: int = Field(default=3600, ge=10, le=86400)
    upload_cleanup_enabled: bool = True
    # A WAITING stage idle longer than this, or a RUNNING stage whose lease expired, can be retriggered.
    stage_stuck_seconds: int = Field(default=900, ge=60, le=86400)
    standardization_stuck_seconds: int = Field(default=1800, ge=60, le=86400)

    # Secret used to sign CSRF tokens and challenge tokens and to encrypt stored refresh tokens.
    # Must be long and random outside development.
    secret_key: str = 'dev-only-secret-change-me-dev-only-secret-change-me'

    # Identity provider: "dev" keeps accounts in the database (local and tests only),
    # "workos" uses WorkOS AuthKit (hosted sign-in, MFA, password reset, invitations, SSO).
    auth_provider: Literal['dev', 'workos'] = 'dev'
    workos_api_key: str = ''
    workos_client_id: str = ''
    workos_api_base: str = 'https://api.workos.com'

    # Public origin of the web app (the API is reached through it at /api).
    # WorkOS redirects back to {app_url}/api/auth/callback.
    app_url: str = 'http://127.0.0.1:4174'

    # MFA is always required in the Admin console outside development. Can be relaxed locally.
    admin_require_mfa: bool = True

    # Sessions
    session_idle_minutes: int = 30
    session_absolute_hours: int = 12
    session_reverify_minutes: int = 15
    cookie_secure: bool = False
    cookie_domain: str | None = None

    cors_origins: list[str] = ['http://127.0.0.1:4173', 'http://127.0.0.1:4174',
                               'http://localhost:4173', 'http://localhost:4174']
    trust_proxy_headers: bool = False
    # Shared secret the Cloudflare Worker adds as X-Lexaior-Proxy on every /api request. When
    # set, the API refuses /api calls without it, so the Render URL can't be used directly
    # (which would also let callers fake X-Forwarded-For and dodge rate limits).
    proxy_secret: str = ''

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings,
                                   dotenv_settings, file_secret_settings):
        # Environment > mounted secrets > local development dotenv > defaults.
        return init_settings, env_settings, file_secret_settings, dotenv_settings

    @field_validator('postgres_password', 'valkey_password', 'storage_access_key', 'storage_secret_key')
    @classmethod
    def reject_placeholder_secrets(cls, value, info):
        secret = value.get_secret_value()
        # The Postgres password may be empty when DATABASE_URL carries the credentials.
        if secret == 'change-me' or (info.field_name != 'postgres_password' and not secret.strip()):
            raise ValueError('Provide a real runtime secret; placeholders are not accepted.')
        return value

    @field_validator('storage_provider')
    @classmethod
    def supported_storage(cls, value):
        if value not in {'seaweedfs', 's3'}:
            raise ValueError('Storage must be SeaweedFS or another S3-compatible service (seaweedfs | s3).')
        return value

    @field_validator('database_dsn')
    @classmethod
    def postgres_dsn(cls, value):
        if value and not value.startswith(('postgresql', 'postgres://')):
            raise ValueError('DATABASE_URL must be a Postgres URL')
        return value

    @model_validator(mode='after')
    def _production_guards(self):
        if not self.database_dsn and not self.postgres_password.get_secret_value():
            raise ValueError('Set DATABASE_URL or POSTGRES_PASSWORD.')
        if self.environment in ('staging', 'production'):
            if self.auth_provider == 'dev':
                raise ValueError(f'AUTH_PROVIDER=dev is not allowed in {self.environment}')
            if self.environment == 'production' and not self.workos_api_key.startswith('sk_live_'):
                raise ValueError('Production needs a WorkOS production key (sk_live_...)')
            if self.secret_key.startswith('dev-only') or len(self.secret_key) < 32:
                raise ValueError('SECRET_KEY must be a long random value outside development')
            if not self.cookie_secure:
                raise ValueError('COOKIE_SECURE must be true outside development')
            if not self.admin_require_mfa:
                raise ValueError('ADMIN_REQUIRE_MFA must be true outside development')
        if self.auth_provider == 'workos' and not (self.workos_api_key and self.workos_client_id):
            raise ValueError('AUTH_PROVIDER=workos needs WORKOS_API_KEY and WORKOS_CLIENT_ID')
        return self

    def _url(self, driver) -> tuple[URL, dict]:
        """URL for one driver plus its query options (sslmode etc.), from DATABASE_URL or the POSTGRES_* parts."""
        if self.database_dsn:
            url = make_url(self.database_dsn.replace('postgres://', 'postgresql://', 1))
            return url.set(drivername=driver, query={}), dict(url.query)
        return URL.create(driver, username=self.postgres_user, password=self.postgres_password.get_secret_value(),
                          host=self.postgres_host, port=self.postgres_port, database=self.postgres_db), {}

    @property
    def database_url(self) -> URL:
        """Async (asyncpg) URL used by the RAG services. asyncpg takes TLS via connect args, not the URL."""
        return self._url('postgresql+asyncpg')[0]

    @property
    def database_connect_args(self) -> dict:
        sslmode = self._url('postgresql+asyncpg')[1].get('sslmode')
        return {'ssl': sslmode} if sslmode and sslmode not in {'disable', 'allow', 'prefer'} else {}

    @property
    def sync_database_url(self) -> URL:
        """Sync (psycopg 3) URL used by the firm/IAM routes, scripts and migrations."""
        url, query = self._url('postgresql+psycopg')
        return url.set(query=query)

    @property
    def dev_auth(self) -> bool:
        return self.auth_provider == 'dev' and self.environment in ('development', 'test')

    @property
    def hosted(self) -> bool:
        return self.environment in ('staging', 'production')


@lru_cache
def get_settings() -> Settings:
    # <repo>/.env wherever the process starts (API, scripts, migrations); real environment variables win.
    default = Path(__file__).resolve().parents[3] / '.env'
    env_file = os.environ.get('RAG_ENV_FILE', str(default))
    return Settings(_env_file=env_file, _secrets_dir=os.environ.get('RAG_SECRETS_DIR'))
