"""HTTP-приложение (FastAPI): проверки состояния; позже — вебхуки и OAuth.

Запуск: uvicorn app.main:create_app --factory
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from app import __version__
from app.api.health import router as health_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import create_engine, create_session_factory


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level, json=settings.log_json)
        engine = create_engine(settings.database_url)
        redis = Redis.from_url(settings.redis_url)
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        app.state.redis = redis
        get_logger(__name__).info("api.started", env=settings.app_env, version=__version__)
        try:
            yield
        finally:
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(title="AI-маркетолог", version=__version__, lifespan=lifespan)
    app.include_router(health_router)
    return app
