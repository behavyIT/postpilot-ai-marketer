"""Подключение Telegram-канала к проекту: пересланный пост, адрес @канала или добавление бота в админы."""

from __future__ import annotations

import re

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.orchestrator import messages as msg
from app.agents.orchestrator.orchestrator import Turn
from app.agents.orchestrator.post_view import CHANNEL_HOWTO
from app.agents.orchestrator.replies import Action, Button, Reply
from app.integrations.social.telegram import ChannelCheckError, TelegramPublisher
from app.services.channels import ChannelService

# @username или ссылка t.me/username — целиком сообщение (иначе это обычный текст со ссылкой).
_CHANNEL_REF = re.compile(
    r"^\s*(?:@|(?:https?://)?(?:t|telegram)\.me/)([A-Za-z][A-Za-z0-9_]{3,31})/?\s*$", re.IGNORECASE
)


def channel_reference(text: str | None) -> str | None:
    """«@my_channel» или «https://t.me/my_channel» → «@my_channel»."""
    if not text:
        return None
    match = _CHANNEL_REF.match(text)
    return f"@{match.group(1)}" if match else None


async def connect_channel(bot: Bot, session: AsyncSession, turn: Turn, chat_ref: int | str) -> Reply:
    if turn.project is None:
        return Reply(text="Канал привязывается к проекту. " + msg.NO_PROJECT, intent="channel_connect")
    try:
        check = await TelegramPublisher(bot).check_channel(chat_ref, requested_by=turn.user.telegram_user_id)
    except ChannelCheckError as exc:
        return Reply(text=f"{exc}\n\n{CHANNEL_HOWTO}", intent="channel_connect")
    except TelegramNetworkError:
        return Reply(text="Telegram не ответил — попробуйте ещё раз через минуту.", intent="channel_connect")

    await ChannelService(session).connect(turn.user, turn.project, check)
    title = msg.q(check.title)
    if not check.can_post:
        return Reply(
            text=(
                f"Канал «{title}» нашёл, но {'; '.join(check.problems)}.\n"
                "Откройте канал → Администраторы → бот → включите «Публикация сообщений» "
                "и пришлите адрес канала ещё раз."
            ),
            intent="channel_connect",
        )
    return Reply(
        text=(
            f"📢 Канал «{title}» подключён к проекту «{msg.q(turn.project.name)}».\n"
            "Публикую туда только одобренные вами посты — сразу или по расписанию."
        ),
        buttons=[
            [Button(text="⏳ Посты на одобрении", action=Action.POST_LIST, value="pending")],
            [Button(text="✅ Одобренные", action=Action.POST_LIST, value="approved")],
        ],
        intent="channel_connect",
    )
