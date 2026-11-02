"""SQLAlchemy 2.0 async database connection management and declarative base."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
import uuid

from sqlalchemy import DateTime, MetaData, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool

from gateway.config.settings import GatewaySettings
from gateway.core.logging import get_logger

logger = get_logger("gateway.db")

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base declarative class for all Universal AI Gateway database models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampUUIDMixin:
    """Standard UUID primary key and UTC timestamp columns for gateway entities."""

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class DatabaseManager:
    """Manages the SQLAlchemy AsyncEngine and AsyncSession factory lifecycle."""

    def __init__(self, settings: GatewaySettings) -> None:
        self._settings = settings
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    def init_engine(self) -> AsyncEngine:
        """Initializes the SQLAlchemy AsyncEngine with explicit pool and timeout settings."""
        if self._engine is not None:
            return self._engine

        engine_kwargs: dict[str, Any] = {
            "echo": self._settings.db_echo,
            "pool_pre_ping": True,
        }

        if self._settings.database_url.startswith("sqlite+aiosqlite://"):
            if ":memory:" in self._settings.database_url:
                engine_kwargs["poolclass"] = StaticPool
            engine_kwargs["connect_args"] = {"check_same_thread": False}
        else:
            engine_kwargs["pool_size"] = self._settings.db_pool_size
            engine_kwargs["max_overflow"] = self._settings.db_max_overflow
            engine_kwargs["pool_timeout"] = self._settings.db_pool_timeout_seconds

        self._engine = create_async_engine(self._settings.database_url, **engine_kwargs)
        self._session_factory = async_sessionmaker(
            bind=self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
        logger.info("database_engine_initialized", driver=self._engine.dialect.name)
        return self._engine

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            return self.init_engine()
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            self.init_engine()
        assert self._session_factory is not None
        return self._session_factory

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Async context manager yielding an AsyncSession that commits on success or rolls back on error."""
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def get_session(self) -> AsyncIterator[AsyncSession]:
        """FastAPI dependency yielding a managed AsyncSession."""
        async with self.session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    async def check_health(self, timeout_seconds: float | None = None) -> dict[str, Any]:
        """Executes a lightweight SELECT 1 probe within an explicit timeout."""
        timeout = timeout_seconds or self._settings.db_pool_timeout_seconds
        try:
            async with asyncio.timeout(timeout):
                async with self.engine.connect() as conn:
                    result = await conn.execute(text("SELECT 1"))
                    scalar = result.scalar_one()
                    return {
                        "status": "healthy" if scalar == 1 else "unhealthy",
                        "dialect": self.engine.dialect.name,
                    }
        except Exception as exc:
            logger.warning("database_health_check_failed", error=str(exc))
            return {
                "status": "unhealthy",
                "dialect": self.engine.dialect.name,
                "error": type(exc).__name__,
            }

    async def dispose(self) -> None:
        """Gracefully closes all pooled database connections."""
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None
            logger.info("database_engine_disposed")

