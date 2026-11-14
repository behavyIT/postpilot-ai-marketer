from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram.types import Chat, Message
from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers import cmd_start
from app.db.models import User
from app.services.access import AccessService, TelegramProfile

pytestmark = pytest.mark.integration


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Перехватываем Message.answer, чтобы не ходить в Telegram."""
    replies: list[str] = []

    async def fake_answer(self: Message, text: str, **_: Any) -> None:
        replies.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)
    return replies


def start_message(telegram_id: int, first_name: str = "Анна") -> Message:
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=telegram_id, type="private"),
        from_user=TgUser(id=telegram_id, is_bot=False, first_name=first_name, language_code="ru"),
        text="/start",
    )


async def test_start_registers_new_user_and_greets(session: AsyncSession, sent: list[str]) -> None:
    await cmd_start(start_message(3001), session)

    user = await session.scalar(select(User).where(User.telegram_user_id == 3001))
    assert user is not None
    assert user.first_name == "Анна"
    assert len(sent) == 1
    assert "Я твой AI-маркетолог" in sent[0]
    assert "ссылку на сайт" in sent[0]


async def test_start_for_returning_user_shows_active_project(session: AsyncSession, sent: list[str]) -> None:
    access = AccessService(session, default_timezone="Europe/Moscow")
    user, _ = await access.register_or_update_user(TelegramProfile(telegram_user_id=3002, first_name="Олег"))
    await access.create_project(user, "Кофейня")

    await cmd_start(start_message(3002, "Олег"), session)

    assert "С возвращением, Олег" in sent[-1]
    assert "<b>Кофейня</b>" in sent[-1]
