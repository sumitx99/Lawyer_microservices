"""Async migrations using runtime credentials; no secrets in alembic.ini."""
import asyncio
from alembic import context
from sqlalchemy.dialects.postgresql import REGCONFIG
from sqlalchemy.dialects.postgresql.base import ischema_names
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from pymicroservices.app.models import Base
from pymicroservices.app.utilities.secrets_control import get_settings

# Reflection does not know regconfig (text_nodes.ts_config); register it so `check` compares it.
ischema_names.setdefault('regconfig', REGCONFIG)

config = context.config
target_metadata = Base.metadata


def migrate(connection):
    context.configure(connection=connection, target_metadata=target_metadata,
                      compare_type=True, compare_server_default=True)
    with context.begin_transaction():
        context.run_migrations()


async def run():
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(migrate)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(url='postgresql+asyncpg://localhost/unused', target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={'paramstyle': 'named'})
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(run())
