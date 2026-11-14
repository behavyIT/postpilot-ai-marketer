"""Запуск Telegram-бота в режиме long polling: python -m app.bot"""

from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand, ErrorEvent, User
from redis.asyncio import Redis

from app.ai.llm.factory import create_llm_service
from app.bot import texts
from app.bot.handlers import build_router
from app.bot.middlewares import DbSessionMiddleware, RateLimitMiddleware, UserContextMiddleware
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import create_engine, create_session_factory
from app.integrations.storage.local import LocalFileStorage
from app.workers.broker import broker
from app.workers.queue import TaskiqJobQueue

COMMANDS = [
    BotCommand(command="start", description="Начало работы"),
    BotCommand(command="help", description="Что я умею"),
    BotCommand(command="project", description="Проекты"),
    BotCommand(command="profile", description="Что я знаю о бизнесе"),
    BotCommand(command="strategy", description="Стратегия продвижения"),
    BotCommand(command="plan", description="Контент-план"),
    BotCommand(command="content", description="Создать публикацию"),
    BotCommand(command="schedule", description="Расписание публикаций"),
    BotCommand(command="analytics", description="Аналитика"),
    BotCommand(command="settings", description="Настройки"),
    BotCommand(command="pause", description="Пауза автоматической работы"),
    BotCommand(command="resume", description="Возобновить работу"),
]

log = get_logger("app.bot")


async def on_error(event: ErrorEvent) -> bool:
    """Пользователь видит короткое сообщение, подробности — только в логе."""
    log.exception("bot.unhandled_error", exc_info=event.exception)
    update = event.update
    try:
        if update.message is not None:
            await update.message.answer(texts.INTERNAL_ERROR)
        elif update.callback_query is not None:
            await update.callback_query.answer(texts.INTERNAL_ERROR, show_alert=True)
    except Exception:
        log.exception("bot.error_reply_failed")
    return True


async def _connect(bot: Bot) -> User:
    """Первое подключение к Telegram с повторами: короткий обрыв связи не должен останавливать бота."""
    delay = 2.0
    while True:
        try:
            return await bot.get_me()
        except TelegramNetworkError as exc:
            log.warning("bot.telegram_unreachable", retry_in_s=delay, error=str(exc)[:200])
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json)

    if settings.telegram_bot_token is None:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан: впишите токен от @BotFather в файл .env")
    if settings.gigachat_auth_key is None:
        raise SystemExit("GIGACHAT_AUTH_KEY не задан: впишите ключ GigaChat в файл .env")

    session = AiohttpSession(proxy=settings.telegram_proxy) if settings.telegram_proxy else AiohttpSession()
    bot = Bot(
        token=settings.telegram_bot_token.get_secret_value(),
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    redis = Redis.from_url(settings.redis_url)
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    llm = create_llm_service(settings, redis=redis, session_factory=session_factory)

    dispatcher = Dispatcher(storage=RedisStorage(redis=redis))
    dispatcher["settings"] = settings
    dispatcher["llm"] = llm
    dispatcher["file_storage"] = LocalFileStorage(settings.files_dir)
    dispatcher.update.outer_middleware(DbSessionMiddleware(session_factory, TaskiqJobQueue(redis)))
    dispatcher.message.outer_middleware(
        RateLimitMiddleware(redis, per_minute=settings.user_messages_per_minute)
    )
    dispatcher.message.middleware(UserContextMiddleware(settings))
    dispatcher.callback_query.middleware(UserContextMiddleware(settings))
    dispatcher.errors.register(on_error)
    dispatcher.include_router(build_router())

    await broker.startup()  # бот ставит задачи в очередь воркера
    try:
        me = await _connect(bot)
        log.info("bot.started", username=me.username)
        await bot.set_my_commands(COMMANDS)
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await broker.shutdown()
        await llm.aclose()
        await redis.aclose()
        await engine.dispose()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
