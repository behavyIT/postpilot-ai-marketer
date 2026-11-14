"""Ввод названия нового проекта (после кнопки «Новый проект» или вопроса бота)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.agents.orchestrator.orchestrator import Orchestrator, Turn
from app.bot.render import send_reply
from app.bot.states import ProjectStates

router = Router(name="projects")


@router.message(ProjectStates.waiting_name, F.text)
async def project_name_entered(
    message: Message, state: FSMContext, turn: Turn, orchestrator: Orchestrator
) -> None:
    reply = await orchestrator.create_project(turn, message.text or "")
    if reply.expect is None:
        await state.clear()
    await send_reply(message, reply, state)
