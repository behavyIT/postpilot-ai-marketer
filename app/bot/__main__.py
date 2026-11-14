"""Запуск Telegram-бота в режиме long polling: python -m app.bot"""

from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from app.bot.handlers import router
from app.bot.middlewares import DbSessionMiddleware
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import create_engine, create_session_factory

COMMANDS = [
    BotCommand(command="start", description="Начало работы"),
    BotCommand(command="help", description="Что я умею"),
]


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json)
    log = get_logger("app.bot")

    if settings.telegram_bot_token is None:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан: впишите токен от @BotFather в файл .env")

    session = AiohttpSession(proxy=settings.telegram_proxy) if settings.telegram_proxy else AiohttpSession()
    bot = Bot(
        token=settings.telegram_bot_token.get_secret_value(),
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    engine = create_engine(settings.database_url)

    dispatcher = Dispatcher()
    dispatcher.update.outer_middleware(DbSessionMiddleware(create_session_factory(engine)))
    dispatcher.include_router(router)

    try:
        me = await bot.get_me()
        log.info("bot.started", username=me.username)
        await bot.set_my_commands(COMMANDS)
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await engine.dispose()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
