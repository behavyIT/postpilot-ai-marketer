"""Подключение к PostgreSQL: движок и фабрика сессий."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def create_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """Создать асинхронный движок; pool_pre_ping отсекает «протухшие» соединения после рестарта БД."""
    return create_async_engine(database_url, echo=echo, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Фабрика сессий. Объекты остаются доступными после commit (expire_on_commit=False)."""
    return async_sessionmaker(engine, expire_on_commit=False)
