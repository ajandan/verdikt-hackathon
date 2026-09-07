from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from docgen.config import get_settings

_settings = get_settings()

# NullPool for the async engine: asyncpg pool connections bind to a specific
# event loop, which trips up test suites that spin fresh loops per TestClient
# and also risks cross-loop reuse in some deployment topologies. Fresh
# connections per session, closed on release. Acceptable overhead here.
async_engine = create_async_engine(
    _settings.database_url,
    pool_pre_ping=True,
    future=True,
    poolclass=NullPool,
)
AsyncSessionLocal = async_sessionmaker(async_engine, expire_on_commit=False, class_=AsyncSession)

sync_engine = create_engine(_settings.database_url_sync, pool_pre_ping=True, future=True)
SyncSessionLocal = sessionmaker(sync_engine, expire_on_commit=False, class_=Session)


@asynccontextmanager
async def async_session() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as s:
        yield s


@contextmanager
def sync_session() -> Iterator[Session]:
    with SyncSessionLocal() as s:
        yield s
