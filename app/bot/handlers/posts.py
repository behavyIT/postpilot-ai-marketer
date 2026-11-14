"""Ответы на вопросы бота про пост: «что изменить?» и «когда публикуем?»."""

from __future__ import annotations

from functools import partial

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.agents.orchestrator.orchestrator import Orchestrator, Turn
from app.agents.orchestrator.replies import Expect
from app.bot.render import send_reply
from app.bot.states import PostStates

router = Router(name="posts")


@router.message(PostStates.waiting_edit, F.text)
async def post_edit_entered(
    message: Message, state: FSMContext, turn: Turn, orchestrator: Orchestrator
) -> None:
    await _answer(message, state, turn, orchestrator, Expect.POST_EDIT)


@router.message(PostStates.waiting_time, F.text)
async def post_time_entered(
    message: Message, state: FSMContext, turn: Turn, orchestrator: Orchestrator
) -> None:
    await _answer(message, state, turn, orchestrator, Expect.POST_TIME)


async def _answer(
    message: Message, state: FSMContext, turn: Turn, orchestrator: Orchestrator, expect: Expect
) -> None:
    data = {k: str(v) for k, v in (await state.get_data()).items()}
    await state.clear()
    reply = await orchestrator.handle_post_input(
        turn, expect, data, message.text or "", html=message.html_text if message.text else None
    )
    await send_reply(message, reply, state, on_sent=partial(orchestrator.presented, turn))
