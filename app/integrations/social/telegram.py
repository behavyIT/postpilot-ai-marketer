"""Публикация в Telegram-канал через Bot API (aiogram).

Бот должен быть администратором канала с правом «Публикация сообщений»; проверка —
getChatMember(канал, бот). Подключить канал к проекту может только администратор
этого канала: иначе любой пользователь бота мог бы публиковать в чужой канал,
где бот уже состоит.

Ограничения Bot API, о которых публикатор говорит честно:
- прочитать сообщение канала по id нельзя (get_post → NotSupportedError);
- просмотров нет; доступны только счётчики реакций — через обновления, этап аналитики.
"""

from __future__ import annotations

from typing import Any

from aiogram import Bot
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.types import BufferedInputFile, Chat, ChatMemberAdministrator, ChatMemberOwner
from aiohttp import ClientConnectorError

from app.domain.enums import Platform
from app.domain.platforms import TELEGRAM, validate_for_platform
from app.integrations.social.base import (
    ChannelCheck,
    NotSupportedError,
    PostContent,
    PublishError,
    PublishErrorKind,
    PublishResult,
)


class ChannelCheckError(Exception):
    """Канал нельзя подключить; текст — для пользователя."""


class TelegramPublisher:
    platform = Platform.TELEGRAM

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def authenticate(self) -> None:
        await self._bot.me()

    def validate_content(self, content: PostContent) -> list[str]:
        return validate_for_platform(content.text, TELEGRAM, with_image=content.image is not None)

    async def check_channel(self, chat_ref: int | str, *, requested_by: int | None) -> ChannelCheck:
        """Права бота в канале и (если указан пользователь) его права администратора."""
        try:
            chat = await self._bot.get_chat(chat_ref)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            raise ChannelCheckError(
                "Не вижу этот канал. Добавьте бота в администраторы канала и попробуйте снова."
            ) from exc
        if chat.type != ChatType.CHANNEL:
            raise ChannelCheckError("Это не канал. Публиковать я умею в Telegram-каналы.")

        if requested_by is not None:
            try:
                requester = await self._bot.get_chat_member(chat.id, requested_by)
            except TelegramAPIError:
                requester = None
            if not isinstance(requester, ChatMemberOwner | ChatMemberAdministrator):
                raise ChannelCheckError("Подключить канал может только его администратор.")

        me = await self._bot.me()
        try:
            member = await self._bot.get_chat_member(chat.id, me.id)
        except (TelegramBadRequest, TelegramForbiddenError):
            member = None
        problems: list[str] = []
        can_post = False
        capabilities: dict[str, Any] = {"read_posts": False, "views": False, "reactions": True}
        if not isinstance(member, ChatMemberAdministrator):
            problems.append("бот не администратор канала")
        else:
            can_post = bool(member.can_post_messages)
            if not can_post:
                problems.append("у бота нет права «Публикация сообщений»")
            capabilities["delete_posts"] = bool(member.can_delete_messages)
            capabilities["edit_posts"] = bool(member.can_edit_messages)
        return ChannelCheck(
            chat_id=chat.id,
            title=chat.title or str(chat.id),
            username=chat.username,
            can_post=can_post,
            problems=problems,
            capabilities=capabilities,
        )

    async def publish(self, target: str, content: PostContent) -> PublishResult:
        chat_id = int(target)
        try:
            if content.image is not None:
                message = await self._bot.send_photo(
                    chat_id,
                    BufferedInputFile(content.image, filename=content.image_name),
                    caption=content.text,
                    parse_mode=ParseMode.HTML,
                )
            else:
                message = await self._bot.send_message(chat_id, content.text, parse_mode=ParseMode.HTML)
        except TelegramRetryAfter as exc:
            raise PublishError(
                PublishErrorKind.RETRYABLE,
                "retry_after",
                "Telegram просит подождать",
                retry_after=exc.retry_after,
            ) from exc
        except TelegramEntityTooLarge as exc:
            raise PublishError(PublishErrorKind.PERMANENT, "too_large", "Картинка слишком большая") from exc
        except (TelegramForbiddenError, TelegramUnauthorizedError) as exc:
            raise PublishError(
                PublishErrorKind.PERMANENT, "forbidden", "Бот потерял доступ к каналу — проверьте его права"
            ) from exc
        except TelegramBadRequest as exc:
            raise PublishError(
                PublishErrorKind.PERMANENT, "bad_request", f"Telegram отклонил пост: {exc.message}"
            ) from exc
        except TelegramServerError as exc:
            # Сервер ответил ошибкой — мог успеть опубликовать. Лучше спросить, чем выпустить дубль.
            raise PublishError(
                PublishErrorKind.UNKNOWN, "server_error", "Telegram ответил ошибкой сервера"
            ) from exc
        except TelegramNetworkError as exc:
            if isinstance(exc.__cause__, ClientConnectorError):
                # Соединение не установлено — запрос до Telegram не дошёл.
                raise PublishError(PublishErrorKind.RETRYABLE, "connect", "Нет связи с Telegram") from exc
            raise PublishError(
                PublishErrorKind.UNKNOWN, "no_response", "Telegram не ответил — пост мог выйти"
            ) from exc
        return PublishResult(
            external_id=str(message.message_id), url=post_url(message.chat, message.message_id)
        )

    async def get_post(self, target: str, external_id: str) -> dict[str, Any]:
        raise NotSupportedError("Bot API не позволяет прочитать сообщение канала по id")

    async def delete_post(self, target: str, external_id: str) -> None:
        try:
            await self._bot.delete_message(int(target), int(external_id))
        except TelegramAPIError as exc:
            raise PublishError(PublishErrorKind.PERMANENT, "delete_failed", str(exc)) from exc

    async def get_statistics(self, target: str, external_id: str) -> dict[str, Any]:
        raise NotSupportedError("Статистика Telegram-канала через Bot API: только реакции, без просмотров")


def post_url(chat: Chat, message_id: int) -> str | None:
    if chat.username:
        return f"https://t.me/{chat.username}/{message_id}"
    raw = str(chat.id)
    if raw.startswith("-100"):
        return f"https://t.me/c/{raw[4:]}/{message_id}"
    return None
