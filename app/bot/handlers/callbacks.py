"""Нажатия inline-кнопок."""

from __future__ import annotations

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.agents.orchestrator.orchestrator import Orchestrator, Turn
from app.agents.orchestrator.replies import Action
from app.bot.keyboards import ActionCallback
from app.bot.render import send_reply

router = Router(name="callbacks")


@router.callback_query(ActionCallback.filter())
async def on_action(
    callback: CallbackQuery,
    callback_data: ActionCallback,
    state: FSMContext,
    turn: Turn,
    orchestrator: Orchestrator,
) -> None:
    try:
        action = Action(callback_data.action)
    except ValueError:
        await callback.answer("Эта кнопка устарела", show_alert=False)
        return
    reply = await orchestrator.handle_button(turn, action, callback_data.value)
    await callback.answer()
    if isinstance(callback.message, Message):
        await send_reply(callback.message, reply, state)
