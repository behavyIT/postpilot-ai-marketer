"""Приём документов и фотографий как источников знаний о бизнесе."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

from app.agents.orchestrator.orchestrator import Orchestrator, Turn
from app.bot import texts
from app.bot.render import send_reply
from app.core.config import Settings

router = Router(name="media")


@router.message(F.document)
async def on_document(
    message: Message, bot: Bot, turn: Turn, orchestrator: Orchestrator, settings: Settings
) -> None:
    document = message.document
    if document is None:  # pragma: no cover — гарантирует фильтр
        return
    if document.file_size and document.file_size > settings.max_upload_mb * 1024 * 1024:
        await message.answer(texts.FILE_TOO_LARGE.format(limit=settings.max_upload_mb))
        return
    async with ChatActionSender.upload_document(bot=bot, chat_id=message.chat.id):
        buffer = await bot.download(document)
        data = buffer.read() if buffer else b""
        reply = await orchestrator.handle_document(
            turn, filename=document.file_name, data=data, caption=message.caption
        )
    await send_reply(message, reply)


@router.message(F.photo)
async def on_photo(
    message: Message, bot: Bot, turn: Turn, orchestrator: Orchestrator, settings: Settings
) -> None:
    photo = message.photo[-1] if message.photo else None  # самое большое разрешение
    if photo is None:  # pragma: no cover
        return
    if photo.file_size and photo.file_size > settings.max_upload_mb * 1024 * 1024:
        await message.answer(texts.FILE_TOO_LARGE.format(limit=settings.max_upload_mb))
        return
    async with ChatActionSender.upload_photo(bot=bot, chat_id=message.chat.id):
        buffer = await bot.download(photo)
        data = buffer.read() if buffer else b""
        filename = f"Фото от {message.date:%d.%m.%Y %H:%M}.jpg"
        reply = await orchestrator.handle_document(
            turn, filename=filename, data=data, caption=message.caption
        )
    await send_reply(message, reply)
