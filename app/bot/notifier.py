"""Отправка сообщений пользователю не в ответ на его сообщение (из воркера).

Связь с Telegram в РФ бывает нестабильной, поэтому при сетевой ошибке отправка
повторяется с паузами: результат многоминутной работы не должен теряться из-за обрыва.
"""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Protocol

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter

from app.agents.orchestrator.replies import Reply
from app.bot.render import send_with_fallback
from app.core.logging import get_logger

log = get_logger(__name__)

SEND_ATTEMPTS = 5


class Notifier(Protocol):
    async def send(self, chat_id: int, reply: Reply) -> None: ...


class TelegramNotifier:
    def __init__(self, bot: Bot, *, base_delay_s: float = 2.0) -> None:
        self._bot = bot
        self._base_delay_s = base_delay_s

    async def send(self, chat_id: int, reply: Reply) -> None:
        for attempt in range(1, SEND_ATTEMPTS + 1):
            try:
                await send_with_fallback(partial(self._bot.send_message, chat_id), reply)
                return
            except TelegramRetryAfter as exc:
                delay: float = exc.retry_after
            except TelegramNetworkError:
                if attempt == SEND_ATTEMPTS:
                    raise
                delay = self._base_delay_s * 2 ** (attempt - 1)
            log.warning("notifier.retry", attempt=attempt, delay_s=delay)
            await asyncio.sleep(delay)


class MemoryNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, Reply]] = []

    async def send(self, chat_id: int, reply: Reply) -> None:
        self.sent.append((chat_id, reply))
