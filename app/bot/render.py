"""Отправка ответа оркестратора в Telegram."""

from __future__ import annotations

from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.agents.orchestrator.replies import Expect, Reply
from app.bot.keyboards import build_keyboard
from app.bot.states import ProjectStates

TELEGRAM_TEXT_LIMIT = 4096

_STATES = {Expect.PROJECT_NAME: ProjectStates.waiting_name}


async def send_reply(message: Message, reply: Reply, state: FSMContext | None = None) -> None:
    text = (
        reply.text if len(reply.text) <= TELEGRAM_TEXT_LIMIT else reply.text[: TELEGRAM_TEXT_LIMIT - 1] + "…"
    )
    await message.answer(text, reply_markup=build_keyboard(reply))
    if state is not None and reply.expect is not None:
        await state.set_state(_STATES[reply.expect])
