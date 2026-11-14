"""Бота добавили в канал администратором, лишили прав или удалили.

Telegram хранит такие события сутки, поэтому они обрабатываются даже после перезапуска бота.
"""

from __future__ import annotations

from aiogram import Bot, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatMemberUpdated
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.orchestrator import messages as msg
from app.agents.orchestrator.orchestrator import Turn
from app.agents.orchestrator.replies import Reply
from app.bot.channels import connect_channel
from app.bot.render import deliver
from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models import User
from app.domain.enums import SocialAccountStatus
from app.services.access import AccessService
from app.services.channels import ChannelService

router = Router(name="membership")
log = get_logger(__name__)


@router.my_chat_member()
async def on_bot_membership_changed(
    update: ChatMemberUpdated, bot: Bot, session: AsyncSession, settings: Settings
) -> None:
    status = update.new_chat_member.status
    log.info(
        "bot.membership_changed",
        chat_id=update.chat.id,
        chat_type=update.chat.type,
        new_status=status,
        changed_by=update.from_user.id,
    )
    if update.chat.type != ChatType.CHANNEL:
        return

    if status == ChatMemberStatus.ADMINISTRATOR:
        access = AccessService(session, default_timezone=settings.default_timezone)
        user = await access.find_user(update.from_user.id)
        if user is None:
            return  # человек ещё не писал боту — написать ему первым бот не может
        turn = Turn(user=user, project=await access.get_active_project(user), chat_id=update.from_user.id)
        await _tell(bot, update.from_user.id, await connect_channel(bot, session, turn, update.chat.id))
        return

    lost = status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}
    accounts = await ChannelService(session).mark_by_chat(
        str(update.chat.id),
        status=SocialAccountStatus.DISCONNECTED if lost else SocialAccountStatus.NO_RIGHTS,
        reason="бота удалили из канала" if lost else "бот больше не администратор канала",
    )
    for account in accounts:
        owner = (
            await session.get(User, account.connected_by_user_id) if account.connected_by_user_id else None
        )
        if owner is not None:
            text = (
                f"⚠️ Я больше не могу публиковать в «{msg.q(account.title)}»: "
                + ("бота удалили из канала." if lost else "бот больше не администратор.")
                + " Запланированные посты не выйдут, пока права не вернутся."
            )
            await _tell(bot, owner.telegram_user_id, Reply(text=text))


async def _tell(bot: Bot, chat_id: int, reply: Reply) -> None:
    try:
        await deliver(bot, chat_id, reply)
    except TelegramAPIError:
        log.warning("bot.membership_notify_failed", chat_id=chat_id)
