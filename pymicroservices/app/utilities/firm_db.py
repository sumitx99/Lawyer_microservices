"""Async SQLAlchemy (asyncpg) sessions for the firm/IAM routes; scripts and migrations use asyncpg too.

There is one engine: the app's `Database` (postgresql_helper.py), shared with the RAG services, so
firm routes and RAG services reach the same database and schema with the same connection settings
(search_path set per connection, no prepared statements). The RAG ORM models can be queried from
these sessions too."""
from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase


class FirmBase(DeclarativeBase):
    pass


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request (FastAPI shares it between the guard and the route). Nothing is
    flushed before an explicit flush/commit, and loaded rows stay readable after commit."""
    async with request.app.state.db.sessions(autoflush=False) as session:
        yield session
