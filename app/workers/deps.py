"""Ресурсы процесса-воркера: создаются при старте и закрываются при остановке."""

from __future__ import annotations

from dataclasses import dataclass

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.ai.llm.factory import create_llm_service
from app.ai.llm.service import LLMService
from app.bot.notifier import Notifier, TelegramNotifier
from app.core.config import Settings
from app.db.session import create_engine, create_session_factory
from app.integrations.storage.local import LocalFileStorage
from app.integrations.web.crawler import SiteCrawler
from app.integrations.web.fetcher import SafeFetcher
from app.services.profile_analysis import ProfileAnalysisService


@dataclass(slots=True)
class WorkerDeps:
    settings: Settings
    engine: AsyncEngine | None
    session_factory: async_sessionmaker[AsyncSession]
    redis: Redis
    llm: LLMService
    fetcher: SafeFetcher | None
    profile_service: ProfileAnalysisService
    notifier: Notifier
    bot: Bot | None = None

    async def aclose(self) -> None:
        await self.llm.aclose()
        if self.fetcher is not None:
            await self.fetcher.aclose()
        if self.bot is not None:
            await self.bot.session.close()
        await self.redis.aclose()
        if self.engine is not None:
            await self.engine.dispose()


async def build_worker_deps(settings: Settings) -> WorkerDeps:
    if settings.telegram_bot_token is None:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан: воркер не сможет отправлять результаты")
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    redis = Redis.from_url(settings.redis_url)
    llm = create_llm_service(settings, redis=redis, session_factory=session_factory)
    fetcher = SafeFetcher()
    session = AiohttpSession(proxy=settings.telegram_proxy) if settings.telegram_proxy else AiohttpSession()
    bot = Bot(
        token=settings.telegram_bot_token.get_secret_value(),
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    profile_service = ProfileAnalysisService(
        session_factory,
        llm=llm,
        storage=LocalFileStorage(settings.files_dir),
        crawler=SiteCrawler(fetcher, max_pages=settings.crawl_max_pages, delay_s=settings.crawl_delay_s),
    )
    return WorkerDeps(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        redis=redis,
        llm=llm,
        fetcher=fetcher,
        profile_service=profile_service,
        notifier=TelegramNotifier(bot),
        bot=bot,
    )
