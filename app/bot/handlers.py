"""Обработчики команд Этапа 2: регистрация и справка.

Полноценный набор команд, проекты и свободный текст появятся на Этапе 3.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import ChatMemberUpdated, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.access import AccessService, TelegramProfile

router = Router(name="core")
log = get_logger(__name__)


def _access(session: AsyncSession) -> AccessService:
    return AccessService(session, default_timezone=get_settings().default_timezone)


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession) -> None:
    sender = message.from_user
    if sender is None or sender.is_bot:
        return
    access = _access(session)
    user, created = await access.register_or_update_user(
        TelegramProfile(
            telegram_user_id=sender.id,
            username=sender.username,
            first_name=sender.first_name,
            last_name=sender.last_name,
            language_code=sender.language_code,
        )
    )
    log.info("bot.start", user_id=str(user.id), created=created)

    if created:
        await message.answer(texts.WELCOME_NEW.format(name=texts.greeting_name(sender.first_name)))
        return
    project = await access.get_active_project(user)
    await message.answer(texts.welcome_back(sender.first_name, project.name if project else None))


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(texts.HELP)


@router.message(F.text)
async def any_text(message: Message) -> None:
    await message.answer(texts.NOT_READY_YET)


@router.my_chat_member()
async def on_bot_membership_changed(update: ChatMemberUpdated) -> None:
    """Бота добавили в канал или чат (или убрали оттуда).

    Пока только логируем: подключение канала к проекту появится на Этапе 6.
    Telegram хранит такие события сутки, поэтому без обработчика они бы потерялись.
    """
    log.info(
        "bot.membership_changed",
        chat_id=update.chat.id,
        chat_type=update.chat.type,
        chat_title=update.chat.title,
        new_status=update.new_chat_member.status,
        changed_by=update.from_user.id,
    )
