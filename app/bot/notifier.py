"""Отправка сообщений пользователю не в ответ на его сообщение (из воркера)."""

from __future__ import annotations

from typing import Protocol

from aiogram import Bot

from app.agents.orchestrator.replies import Reply
from app.bot.keyboards import build_keyboard
from app.bot.render import TELEGRAM_TEXT_LIMIT


class Notifier(Protocol):
    async def send(self, chat_id: int, reply: Reply) -> None: ...


class TelegramNotifier:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send(self, chat_id: int, reply: Reply) -> None:
        text = (
            reply.text
            if len(reply.text) <= TELEGRAM_TEXT_LIMIT
            else reply.text[: TELEGRAM_TEXT_LIMIT - 1] + "…"
        )
        await self._bot.send_message(chat_id, text, reply_markup=build_keyboard(reply))


class MemoryNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, Reply]] = []

    async def send(self, chat_id: int, reply: Reply) -> None:
        self.sent.append((chat_id, reply))
