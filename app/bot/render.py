"""Отправка ответа оркестратора в Telegram.

Надёжность важнее красоты: если Telegram не принял HTML-разметку (например, модель
написала «5 < 10»), сообщение уходит обычным текстом, а не теряется.
"""

from __future__ import annotations

import html
import re
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.agents.orchestrator.replies import Expect, Reply
from app.bot.keyboards import build_keyboard
from app.bot.states import ProjectStates

TELEGRAM_TEXT_LIMIT = 4096

_STATES = {Expect.PROJECT_NAME: ProjectStates.waiting_name}
_TAG_RE = re.compile(r"<[^>]+>")


def fit_text(text: str) -> str:
    return text if len(text) <= TELEGRAM_TEXT_LIMIT else text[: TELEGRAM_TEXT_LIMIT - 1] + "…"


def as_plain(text: str) -> str:
    """Текст без разметки: теги убраны, сущности вроде &lt; раскрыты."""
    return fit_text(html.unescape(_TAG_RE.sub("", text)))


async def send_with_fallback(send: Callable[..., Awaitable[Any]], reply: Reply) -> None:
    keyboard = build_keyboard(reply)
    try:
        await send(fit_text(reply.text), reply_markup=keyboard)
    except TelegramBadRequest as exc:
        if "parse entities" not in str(exc):
            raise
        await send(as_plain(reply.text), reply_markup=keyboard, parse_mode=None)


async def send_reply(message: Message, reply: Reply, state: FSMContext | None = None) -> None:
    await send_with_fallback(message.answer, reply)
    if state is not None and reply.expect is not None:
        await state.set_state(_STATES[reply.expect])
