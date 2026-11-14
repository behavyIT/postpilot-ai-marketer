"""Подделки внешних систем для тестов (Telegram Bot API без сети)."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from aiogram.methods import SendMessage

METHOD = SendMessage(chat_id=1, text="x")


@dataclass
class FakeBot:
    """Записывает отправленное; может падать заданными исключениями по очереди."""

    errors: list[Exception] = field(default_factory=list)
    sent: list[dict[str, Any]] = field(default_factory=list)
    next_id: int = 100
    chat_username: str | None = "zerno_kzn"

    def _message(self, chat_id: int) -> SimpleNamespace:
        self.next_id += 1
        return SimpleNamespace(
            message_id=self.next_id, chat=SimpleNamespace(id=chat_id, username=self.chat_username)
        )

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> SimpleNamespace:
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append({"kind": "text", "chat_id": chat_id, "text": text, **kwargs})
        return self._message(chat_id)

    async def send_photo(self, chat_id: int, photo: Any, **kwargs: Any) -> SimpleNamespace:
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append({"kind": "photo", "chat_id": chat_id, "photo": photo, **kwargs})
        return self._message(chat_id)
