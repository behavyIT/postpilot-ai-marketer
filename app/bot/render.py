"""Отправка ответа оркестратора в Telegram.

Надёжность важнее красоты: если Telegram не принял HTML-разметку (например, модель
написала «5 < 10»), сообщение уходит обычным текстом, а не теряется.
Пост с картинкой уходит фотографией с подписью; если подпись не помещается
в лимит Telegram (1024 символа), картинка и текст идут двумя сообщениями.
"""

from __future__ import annotations

import html
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, Message

from app.agents.orchestrator.replies import Expect, Reply
from app.bot.keyboards import build_keyboard
from app.bot.states import PostStates, ProjectStates

TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024

_STATES = {
    Expect.PROJECT_NAME: ProjectStates.waiting_name,
    Expect.POST_EDIT: PostStates.waiting_edit,
    Expect.POST_TIME: PostStates.waiting_time,
}
_TAG_RE = re.compile(r"<[^>]+>")

Presented = Callable[[Reply, list[int]], Awaitable[None]]


def fit_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def as_plain(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> str:
    """Текст без разметки: теги убраны, сущности вроде &lt; раскрыты."""
    return fit_text(html.unescape(_TAG_RE.sub("", text)), limit)


def visible_length(text: str) -> int:
    return len(html.unescape(_TAG_RE.sub("", text)))


async def _with_fallback(
    send: Callable[..., Awaitable[Message]], field: str, text: str, keyboard: InlineKeyboardMarkup | None
) -> Message:
    limit = TELEGRAM_CAPTION_LIMIT if field == "caption" else TELEGRAM_TEXT_LIMIT
    try:
        return await send(**{field: fit_text(text, limit)}, reply_markup=keyboard)
    except TelegramBadRequest as exc:
        if "parse entities" not in str(exc):
            raise
        return await send(**{field: as_plain(text, limit)}, reply_markup=keyboard, parse_mode=None)


async def deliver(bot: Bot, chat_id: int, reply: Reply) -> list[int]:
    """Одно сообщение ответа (без followups). Возвращает id отправленных сообщений."""
    keyboard = build_keyboard(reply)
    sent: list[int] = []
    if reply.image is not None:
        photo = BufferedInputFile(reply.image, filename=reply.image_name)
        if visible_length(reply.text) <= TELEGRAM_CAPTION_LIMIT:
            message = await _with_fallback(
                lambda **kw: bot.send_photo(chat_id, photo, **kw), "caption", reply.text, keyboard
            )
            return [message.message_id]
        message = await bot.send_photo(chat_id, photo)
        sent.append(message.message_id)
    message = await _with_fallback(lambda **kw: bot.send_message(chat_id, **kw), "text", reply.text, keyboard)
    sent.append(message.message_id)
    return sent


async def deliver_all(bot: Bot, chat_id: int, reply: Reply, on_sent: Presented | None = None) -> None:
    """Ответ и все сообщения следом (followups), с отметкой показанных превью."""
    for part in [reply, *reply.followups]:
        ids = await deliver(bot, chat_id, part)
        if on_sent is not None:
            await on_sent(part, ids)


async def send_reply(
    message: Message,
    reply: Reply,
    state: FSMContext | None = None,
    *,
    on_sent: Presented | None = None,
) -> None:
    bot = message.bot
    if bot is None:  # pragma: no cover — у входящих сообщений бот всегда есть
        return
    await deliver_all(bot, message.chat.id, reply, on_sent)
    if state is not None and reply.expect is not None:
        await state.set_state(_STATES[reply.expect])
        await state.set_data(dict(reply.expect_data))


async def close_buttons(message: Any) -> None:
    """Убрать кнопки у сообщения, на котором нажали кнопку: решение принято, повторно жать нечего."""
    if isinstance(message, Message) and message.reply_markup is not None:
        with suppress(TelegramBadRequest):  # сообщение старше 48 часов или уже без кнопок
            await message.edit_reply_markup(reply_markup=None)
