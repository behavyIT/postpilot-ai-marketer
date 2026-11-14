"""Отправка сообщений пользователю не в ответ на его сообщение (из воркера).

Связь с Telegram в РФ бывает нестабильной, поэтому при сетевой ошибке отправка
повторяется с паузами: результат многоминутной работы не должен теряться из-за обрыва.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter

from app.agents.orchestrator.replies import Reply
from app.bot.render import deliver
from app.core.logging import get_logger

log = get_logger(__name__)

SEND_ATTEMPTS = 5


class Notifier(Protocol):
    async def send(self, chat_id: int, reply: Reply) -> list[int]:
        """Отправить одно сообщение (followups не отправляются); id отправленных сообщений."""
        ...


class TelegramNotifier:
    def __init__(self, bot: Bot, *, base_delay_s: float = 2.0) -> None:
        self._bot = bot
        self._base_delay_s = base_delay_s

    async def send(self, chat_id: int, reply: Reply) -> list[int]:
        for attempt in range(1, SEND_ATTEMPTS + 1):
            try:
                return await deliver(self._bot, chat_id, reply)
            except TelegramRetryAfter as exc:
                delay: float = exc.retry_after
            except TelegramNetworkError:
                if attempt == SEND_ATTEMPTS:
                    raise
                delay = self._base_delay_s * 2 ** (attempt - 1)
            log.warning("notifier.retry", attempt=attempt, delay_s=delay)
            await asyncio.sleep(delay)
        return []  # pragma: no cover — последний сбой поднимается выше


class MemoryNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, Reply]] = []
        self._next_id = 1000

    async def send(self, chat_id: int, reply: Reply) -> list[int]:
        self.sent.append((chat_id, reply))
        self._next_id += 1
        return [self._next_id]
