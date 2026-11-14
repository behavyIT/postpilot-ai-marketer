"""Бота добавили в канал или чат (или убрали оттуда)."""

from __future__ import annotations

from aiogram import Router
from aiogram.types import ChatMemberUpdated

from app.core.logging import get_logger

router = Router(name="membership")
log = get_logger(__name__)


@router.my_chat_member()
async def on_bot_membership_changed(update: ChatMemberUpdated) -> None:
    """Пока только логируем: подключение канала к проекту появится на этапе публикаций.

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
