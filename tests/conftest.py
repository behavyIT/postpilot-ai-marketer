"""Общие фикстуры.

Интеграционные тесты работают с отдельной базой aimarketer_test (TEST_DATABASE_URL)
из docker compose. Перед прогоном схема пересоздаётся миграциями, каждый тест
выполняется в транзакции, которая откатывается в конце.
"""

from __future__ import annotations

import os

# До импорта приложения: тестовое окружение (брокер задач в памяти и т.п.).
os.environ["APP_ENV"] = "test"

from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import BASE_DIR, Settings, get_settings
from app.db.session import create_engine

get_settings.cache_clear()


def alembic_config(database_url: str) -> Config:
    config = Config(str(BASE_DIR / "alembic.ini"))
    config.attributes["database_url"] = database_url
    config.attributes["configure_logger"] = False
    return config


def run_alembic(action: Callable[..., None], database_url: str, *args: str) -> None:
    """Alembic env.py вызывает asyncio.run, поэтому запускаем его в отдельном потоке."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(action, alembic_config(database_url), *args).result()


@pytest.fixture(scope="session")
def settings() -> Settings:
    return get_settings()


@pytest.fixture(scope="session")
def migrated_database_url(settings: Settings) -> str:
    url = settings.test_database_url
    run_alembic(command.downgrade, url, "base")
    run_alembic(command.upgrade, url, "head")
    return url


@pytest.fixture(scope="session")
async def engine(migrated_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(migrated_database_url)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Сессия внутри внешней транзакции: commit в коде становится SAVEPOINT, в конце — откат."""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        db_session = AsyncSession(
            bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        try:
            yield db_session
        finally:
            await db_session.close()
            await transaction.rollback()
