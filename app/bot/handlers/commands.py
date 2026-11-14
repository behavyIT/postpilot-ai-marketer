"""Команды меню. Каждая команда — то же намерение, что и в свободном тексте, но без модели."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.agents.orchestrator.intents import Intent
from app.agents.orchestrator.orchestrator import Orchestrator, Turn
from app.bot import texts
from app.bot.render import send_reply

router = Router(name="commands")

COMMAND_INTENTS: dict[str, Intent] = {
    "help": Intent.HELP,
    "project": Intent.LIST_PROJECTS,
    "profile": Intent.SHOW_PROFILE,
    "strategy": Intent.CREATE_STRATEGY,
    "plan": Intent.CREATE_PLAN,
    "content": Intent.CREATE_POST,
    "schedule": Intent.SHOW_SCHEDULE,
    "analytics": Intent.ANALYTICS,
    "settings": Intent.SETTINGS,
    "pause": Intent.PAUSE,
    "resume": Intent.RESUME,
}


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, turn: Turn, user_created: bool) -> None:
    await state.clear()
    first_name = message.from_user.first_name if message.from_user else None
    if user_created:
        await message.answer(texts.WELCOME_NEW.format(name=texts.greeting_name(first_name)))
        return
    await message.answer(texts.welcome_back(first_name, turn.project.name if turn.project else None))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(texts.CANCELLED)


@router.message(Command(*COMMAND_INTENTS))
async def menu_command(
    message: Message, command: CommandObject, state: FSMContext, turn: Turn, orchestrator: Orchestrator
) -> None:
    await state.clear()
    reply = await orchestrator.handle_command(turn, COMMAND_INTENTS[command.command], message.text or "")
    await send_reply(message, reply, state)
