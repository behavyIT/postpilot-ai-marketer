"""Сборка LLMService из настроек."""

from __future__ import annotations

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.llm.base import ModelProfile
from app.ai.llm.gigachat import GigaChatProvider
from app.ai.llm.limiter import RedisLimiter
from app.ai.llm.service import LLMService
from app.ai.llm.usage import SqlUsageSink
from app.core.config import Settings


def create_llm_service(
    settings: Settings,
    *,
    redis: Redis,
    session_factory: async_sessionmaker[AsyncSession],
) -> LLMService:
    return LLMService(
        GigaChatProvider(settings),
        models={
            ModelProfile.SMART: settings.gigachat_model_smart,
            ModelProfile.FAST: settings.gigachat_model_fast,
            ModelProfile.VISION: settings.gigachat_model_vision,
        },
        limiter=RedisLimiter(
            redis, "llm:gigachat:slot", ttl_s=settings.llm_lock_ttl_s, wait_s=settings.llm_queue_wait_s
        ),
        usage=SqlUsageSink(session_factory),
    )
