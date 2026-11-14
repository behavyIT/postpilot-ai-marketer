from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, MessageEntity
from aiogram.types import User as TgUser
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.orchestrator.replies import Action, Button, Expect, Reply
from app.ai.llm.fake import FakeLLMProvider
from app.bot.handlers.commands import cmd_start, menu_command
from app.bot.handlers.text import extract_urls
from app.bot.keyboards import ActionCallback, build_keyboard
from app.bot.middlewares import RateLimitMiddleware
from app.bot.render import send_reply
from app.bot.states import ProjectStates
from app.core.config import Settings
from tests.integration.helpers import make_orchestrator, make_user, turn_for

pytestmark = pytest.mark.integration


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Перехватываем Message.answer, чтобы не ходить в Telegram."""
    replies: list[dict[str, Any]] = []

    async def fake_answer(self: Message, text: str, **kwargs: Any) -> None:
        replies.append({"text": text, **kwargs})

    monkeypatch.setattr(Message, "answer", fake_answer)
    return replies


def fsm() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))


def message(text: str, telegram_id: int = 1, entities: list[MessageEntity] | None = None) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=telegram_id, type="private"),
        from_user=TgUser(id=telegram_id, is_bot=False, first_name="Анна"),
        text=text,
        entities=entities,
    )


async def test_start_greets_new_and_returning_user(
    session: AsyncSession, test_settings: Settings, sent: list[dict[str, Any]]
) -> None:
    user = await make_user(session, 6001)
    turn = await turn_for(session, user)

    await cmd_start(message("/start", 6001), fsm(), turn, user_created=True)
    assert "Я твой AI-маркетолог" in sent[-1]["text"]

    orchestrator = make_orchestrator(session, test_settings, FakeLLMProvider())
    await orchestrator.create_project(turn, "Кофейня")
    await cmd_start(message("/start", 6001), fsm(), turn, user_created=False)
    assert "С возвращением, Анна" in sent[-1]["text"]
    assert "<b>Кофейня</b>" in sent[-1]["text"]


async def test_menu_command_runs_intent_without_model(
    session: AsyncSession, test_settings: Settings, sent: list[dict[str, Any]]
) -> None:
    user = await make_user(session, 6002)
    provider = FakeLLMProvider()
    orchestrator = make_orchestrator(session, test_settings, provider)
    state = fsm()
    await state.set_state(ProjectStates.waiting_name)

    command = CommandObject(prefix="/", command="plan")
    await menu_command(message("/plan", 6002), command, state, await turn_for(session, user), orchestrator)

    assert provider.requests == []
    assert "мне нужно узнать бизнес" in sent[-1]["text"]  # без профиля план не строится
    assert await state.get_state() is None  # команда сбрасывает ожидание ввода


async def test_reply_with_expectation_sets_fsm_state(sent: list[dict[str, Any]]) -> None:
    state = fsm()
    reply = Reply(
        text="Как назвать?",
        expect=Expect.PROJECT_NAME,
        buttons=[[Button(text="➕ Новый проект", action=Action.PROJECT_NEW)]],
    )
    await send_reply(message("x"), reply, state)

    assert await state.get_state() == ProjectStates.waiting_name.state
    markup = sent[-1]["reply_markup"]
    callback = markup.inline_keyboard[0][0].callback_data
    assert ActionCallback.unpack(callback).action == "project.new"


def test_callback_data_fits_telegram_limit() -> None:
    reply = Reply(
        text="x",
        buttons=[
            [Button(text="p", action=Action.PROJECT_SWITCH, value="0196f1e2-7c3a-7d4e-8f90-123456789abc")]
        ],
    )
    markup = build_keyboard(reply)
    assert markup is not None
    data = markup.inline_keyboard[0][0].callback_data
    assert data is not None and len(data.encode()) <= 64


def test_urls_are_extracted_from_telegram_entities() -> None:
    text = "🔥 Сайт coffee.ru и меню"
    offset = len("🔥 Сайт ".encode("utf-16-le")) // 2  # Telegram считает смещения в UTF-16
    entities = [
        MessageEntity(type="url", offset=offset, length=len("coffee.ru")),
        MessageEntity(type="text_link", offset=0, length=2, url="https://menu.coffee.ru/"),
        MessageEntity(type="url", offset=offset, length=len("coffee.ru")),
    ]
    assert extract_urls(message(text, entities=entities)) == ["coffee.ru", "https://menu.coffee.ru/"]


async def test_rate_limit_warns_once_and_then_drops(redis_client: Redis, sent: list[dict[str, Any]]) -> None:
    middleware = RateLimitMiddleware(redis_client, per_minute=2)
    handled: list[str] = []

    async def handler(event: Message, data: dict[str, Any]) -> None:
        handled.append(event.text or "")

    for i in range(5):
        event = message(f"сообщение {i}", telegram_id=6003)
        await middleware(handler, event, {"event_from_user": event.from_user})  # type: ignore[arg-type]

    assert handled == ["сообщение 0", "сообщение 1"]
    assert len(sent) == 1
    assert "Слишком много сообщений" in sent[0]["text"]
