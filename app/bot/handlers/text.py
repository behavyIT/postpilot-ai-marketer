"""Свободный текст: оркестратор распознаёт намерение и выполняет действие."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

from app.agents.orchestrator.orchestrator import Orchestrator, Turn
from app.bot import texts
from app.bot.render import send_reply

router = Router(name="text")


def extract_urls(message: Message) -> list[str]:
    """Ссылки, которые распознал сам Telegram (учитывает смещения в UTF-16)."""
    if not message.text or not message.entities:
        return []
    urls: list[str] = []
    for entity in message.entities:
        if entity.type == "url":
            urls.append(entity.extract_from(message.text))
        elif entity.type == "text_link" and entity.url:
            urls.append(entity.url)
    return list(dict.fromkeys(urls))  # без повторов, порядок сохранён


@router.message(F.text)
async def on_text(
    message: Message, bot: Bot, state: FSMContext, turn: Turn, orchestrator: Orchestrator
) -> None:
    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        reply = await orchestrator.handle_text(turn, message.text or "", urls=extract_urls(message))
    await send_reply(message, reply, state)


@router.message()
async def on_unsupported(message: Message) -> None:
    await message.answer(texts.UNSUPPORTED_MESSAGE)
