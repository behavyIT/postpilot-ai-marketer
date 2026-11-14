"""Подключение канала: пересланный из канала пост или адрес @канала."""

from __future__ import annotations

from functools import partial

from aiogram import Bot, F, Router
from aiogram.types import Message, MessageOriginChannel
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.orchestrator.orchestrator import Orchestrator, Turn
from app.bot.channels import channel_reference, connect_channel
from app.bot.render import send_reply

router = Router(name="channels")


@router.message(F.forward_origin.type == "channel")
async def on_forwarded_from_channel(
    message: Message, bot: Bot, session: AsyncSession, turn: Turn, orchestrator: Orchestrator
) -> None:
    origin = message.forward_origin
    if not isinstance(origin, MessageOriginChannel):  # pragma: no cover — гарантирует фильтр
        return
    reply = await connect_channel(bot, session, turn, origin.chat.id)
    await send_reply(message, reply, on_sent=partial(orchestrator.presented, turn))


@router.message(F.text.func(channel_reference))
async def on_channel_reference(
    message: Message, bot: Bot, session: AsyncSession, turn: Turn, orchestrator: Orchestrator
) -> None:
    reference = channel_reference(message.text)
    if reference is None:  # pragma: no cover — гарантирует фильтр
        return
    reply = await connect_channel(bot, session, turn, reference)
    await send_reply(message, reply, on_sent=partial(orchestrator.presented, turn))
