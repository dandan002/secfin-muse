"""Async SQLAlchemy over Postgres (production) or SQLite (zero-setup local dev)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import JSON
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


_engine: AsyncEngine | None = None
_sessions: async_sessionmaker[AsyncSession] | None = None


def configure(url: str) -> AsyncEngine:
    global _engine, _sessions
    kw: dict[str, Any] = {"connect_args": {"timeout": 30}} if url.startswith("sqlite") else {"pool_pre_ping": True}
    _engine = create_async_engine(url, **kw)
    _sessions = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def create_all() -> None:
    from . import models  # noqa: F401 - register tables

    assert _engine is not None
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def dispose() -> None:
    if _engine is not None:
        await _engine.dispose()


def session() -> AsyncSession:
    assert _sessions is not None, "db.configure() first"
    return _sessions()


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with session() as s:
        yield s
