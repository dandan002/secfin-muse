"""Per-service Postgres schema with async SQLAlchemy."""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, MetaData, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# JSONB on Postgres, plain JSON elsewhere.
JsonType = JSON().with_variant(JSONB(), "postgresql")


def make_base(schema: str) -> type[DeclarativeBase]:
    """Declarative base whose tables all live in ``schema``, plus a ``service_meta`` k/v table."""

    class Base(DeclarativeBase):
        metadata = MetaData(schema=schema)
        type_annotation_map = {dict[str, Any]: JsonType, list[Any]: JsonType}

    class ServiceMeta(Base):
        __tablename__ = "service_meta"
        key: Mapped[str] = mapped_column(String(64), primary_key=True)
        value: Mapped[dict[str, Any]] = mapped_column()

    Base.ServiceMeta = ServiceMeta  # type: ignore[attr-defined]
    return Base


class Database:
    def __init__(self, url: str, schema: str, metadata: MetaData) -> None:
        self.url = url
        self.schema = schema
        self.metadata = metadata
        self.engine: AsyncEngine = create_async_engine(url, pool_size=10, max_overflow=20, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    def session(self) -> AsyncSession:
        return self.sessions()

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.exec_driver_sql(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
            await conn.run_sync(self.metadata.create_all)

    async def truncate(self) -> None:
        names = ", ".join(f'"{self.schema}"."{t.name}"' for t in self.metadata.sorted_tables)
        if not names:
            return
        async with self.engine.begin() as conn:
            await conn.exec_driver_sql(f"TRUNCATE {names} RESTART IDENTITY CASCADE")

    async def dispose(self) -> None:
        await self.engine.dispose()
