"""Migrations for both table sets (firm/IAM tables and the RAG tables), one revision chain.

Runs on asyncpg (the service's only driver) with runtime credentials; no secrets in alembic.ini. Every table
lives in DB_SCHEMA (default `public`, as before the firm API; set DB_SCHEMA for a private schema)."""
import asyncio

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.dialects.postgresql import REGCONFIG
from sqlalchemy.dialects.postgresql.base import ischema_names

from pymicroservices.app import firm_models  # noqa: F401  registers the firm tables
from pymicroservices.app.models import Base
from pymicroservices.app.utilities.firm_db import FirmBase
from pymicroservices.app.utilities.secrets_control import get_settings

# Reflection does not know regconfig (text_nodes.ts_config); register it so `check` compares it.
ischema_names.setdefault('regconfig', REGCONFIG)

config = context.config
settings = get_settings()
target_metadata = [FirmBase.metadata, Base.metadata]
SCHEMA = settings.db_schema


def _prepare_postgres(connection):
    """Private schema, pgvector in `extensions`, and no access for Supabase's anon/authenticated roles."""
    connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"'))
    connection.execute(text('CREATE SCHEMA IF NOT EXISTS extensions'))
    connection.execute(text('CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions'))
    connection.execute(text(f"""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            EXECUTE 'REVOKE ALL ON SCHEMA "{SCHEMA}" FROM anon, authenticated';
            EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA "{SCHEMA}" REVOKE ALL ON TABLES FROM anon, authenticated';
          END IF;
        END $$;"""))
    connection.execute(text(f'SET search_path TO "{SCHEMA}", extensions, public'))
    connection.commit()


def _not_version_table(obj, name, type_, reflected, compare_to):
    # The private schema is on the search path, so `check` would otherwise see alembic's own table.
    return not (type_ == 'table' and name == 'alembic_version')


def _migrate(connection):
    _prepare_postgres(connection)
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True,
                      version_table_schema=SCHEMA, include_object=_not_version_table)
    with context.begin_transaction():
        context.run_migrations()


async def run_online():
    # No statement cache: migrations may run through a transaction pooler (Supabase).
    engine = create_async_engine(settings.database_url, poolclass=pool.NullPool, hide_parameters=True,
                                 connect_args={'statement_cache_size': 0, **settings.database_connect_args})
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_migrate)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(url='postgresql+asyncpg://localhost/unused', target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={'paramstyle': 'named'})
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(run_online())
