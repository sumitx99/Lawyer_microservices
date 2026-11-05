"""Sync SQLAlchemy (psycopg 3) for the firm/IAM routes, scripts and migrations.

The RAG services use the async engine in postgresql_helper.py; both reach the same database and
schema. The RAG ORM models can be queried from these sync sessions too."""
from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from pymicroservices.app.utilities.secrets_control import get_settings


class FirmBase(DeclarativeBase):
    pass


def _make_engine(url):
    s = get_settings()
    # Supabase: use the *session* pooler (port 5432). The transaction pooler (6543) breaks
    # prepared statements. TLS is required by Supabase and on by default for its hosts.
    engine = create_engine(url, pool_pre_ping=s.db_pool_pre_ping, pool_size=s.db_pool_size, max_overflow=s.db_pool_size,
                           pool_recycle=1800, hide_parameters=True, connect_args={"prepare_threshold": None})
    schema = s.db_schema

    @event.listens_for(engine, "connect")
    def _search_path(dbapi_connection, _record):
        # Every table lives in the private schema; `public` stays empty for Supabase's Data API.
        with dbapi_connection.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}", extensions, public')
        dbapi_connection.commit()

    return engine


_engine = None
_sessions = None


def get_engine():
    """Created on first use, so importing the app (e.g. for its OpenAPI schema) needs no database settings."""
    global _engine
    if _engine is None:
        _engine = _make_engine(get_settings().sync_database_url)
    return _engine


def __getattr__(name):
    if name == "engine":
        return get_engine()
    raise AttributeError(name)


def SessionLocal() -> Session:  # noqa: N802  (kept as the sessionmaker name every caller uses)
    global _sessions
    if _sessions is None:
        _sessions = sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)
    return _sessions()


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
