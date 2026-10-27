"""Validated runtime settings. No clients or network connections are created here."""

from functools import lru_cache
import os
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra='ignore', case_sensitive=False, hide_input_in_errors=True)

    postgres_host: str = '127.0.0.1'
    postgres_port: int = Field(default=5433, ge=1, le=65535)
    postgres_db: str = 'lexaior_rag'
    postgres_user: str = 'lexaior'
    postgres_password: SecretStr
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=5, ge=0, le=50)
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
    storage_provider: str = 'seaweedfs'
    storage_endpoint_url: str = 'http://127.0.0.1:8333'
    storage_access_key: SecretStr
    storage_secret_key: SecretStr
    storage_bucket: str = 'rag-documents'
    storage_region: str = 'us-east-1'
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
    # Folder uploads: browser PUTs straight to SeaweedFS with a presigned URL bound to size, type and SHA-256.
    thread_max_files: int = Field(default=100, ge=1, le=1000)
    thread_max_file_bytes: int = Field(default=50 * 1024 * 1024, ge=1024, le=500 * 1024 * 1024)
    presign_expiry_seconds: int = Field(default=900, ge=60, le=86400)
    upload_abandon_seconds: int = Field(default=86400, ge=300, le=7 * 86400)
    upload_cleanup_interval_seconds: int = Field(default=3600, ge=10, le=86400)
    upload_cleanup_enabled: bool = True
    # A WAITING stage idle longer than this, or a RUNNING stage whose lease expired, can be retriggered.
    stage_stuck_seconds: int = Field(default=900, ge=60, le=86400)
    standardization_stuck_seconds: int = Field(default=1800, ge=60, le=86400)

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings,
                                   dotenv_settings, file_secret_settings):
        # Environment > mounted secrets > local development dotenv > defaults.
        return init_settings, env_settings, file_secret_settings, dotenv_settings

    @field_validator('postgres_password', 'valkey_password', 'storage_access_key', 'storage_secret_key')
    @classmethod
    def reject_placeholder_secrets(cls, value):
        if not value.get_secret_value().strip() or value.get_secret_value() == 'change-me':
            raise ValueError('Provide a real runtime secret; placeholders are not accepted.')
        return value

    @field_validator('storage_provider')
    @classmethod
    def supported_storage(cls, value):
        if value != 'seaweedfs':
            raise ValueError('Only SeaweedFS is configured in RAG v1.')
        return value

    @property
    def database_url(self) -> URL:
        return URL.create('postgresql+asyncpg', username=self.postgres_user,
                          password=self.postgres_password.get_secret_value(),
                          host=self.postgres_host, port=self.postgres_port, database=self.postgres_db)


@lru_cache
def get_settings() -> Settings:
    default = Path(__file__).resolve().parents[2] / '.env'
    env_file = os.environ.get('RAG_ENV_FILE', str(default))
    return Settings(_env_file=env_file, _secrets_dir=os.environ.get('RAG_SECRETS_DIR'))
