"""Engine, session factory and schema creation.

SQLite on purpose: a channel with a few thousand subscribers is nowhere near
its limits, and "no database to install" is what makes the one-command deploy
possible. DATABASE_URL accepts Postgres too if a customer outgrows it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker | None = None


def _ensure_sqlite_dir(database_url: str) -> None:
    """Create the folder for a file-backed SQLite database if it is missing."""
    marker = "sqlite+aiosqlite:///"
    if not database_url.startswith(marker):
        return
    raw_path = database_url[len(marker) :]
    if not raw_path or raw_path.startswith(":memory:"):
        return
    path = Path(raw_path)
    if path.parent and str(path.parent) not in {"", "."}:
        path.parent.mkdir(parents=True, exist_ok=True)


def get_engine(database_url: str) -> AsyncEngine:
    global _engine
    if _engine is None:
        _ensure_sqlite_dir(database_url)
        _engine = create_async_engine(database_url, echo=False, pool_pre_ping=True)
    return _engine


def get_sessionmaker(database_url: str) -> async_sessionmaker:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(database_url), expire_on_commit=False, autoflush=False
        )
    return _sessionmaker


async def init_db(database_url: str) -> None:
    """Create tables if absent.

    Schema changes ship as additive columns only, so create_all is enough and
    the customer never has to run a migration tool.
    """
    from gatekit.db import models  # noqa: F401  (register mappers)

    engine = get_engine(database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("Database ready at %s", database_url)


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
