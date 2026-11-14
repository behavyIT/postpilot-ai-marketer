"""Middleware бота: сессия БД, пользователь и контекст, ограничение частоты."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject
from aiogram.types import User as TgUser
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.orchestrator.orchestrator import Orchestrator, Turn
from app.bot import texts
from app.core.config import Settings
from app.services.access import AccessService, TelegramProfile
from app.services.jobs import DeferredJobQueue, JobQueue

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class DbSessionMiddleware(BaseMiddleware):
    """Сессия на одно обновление: commit при успехе, откат при исключении.

    Фоновые задачи, поставленные обработчиком, уходят в очередь только после commit:
    воркер не должен начать работу раньше, чем данные окажутся в базе.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], jobs: JobQueue) -> None:
        self._session_factory = session_factory
        self._jobs = jobs

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        deferred = DeferredJobQueue(self._jobs)
        async with self._session_factory() as session:
            data["session"] = session
            data["jobs"] = deferred
            try:
                result = await handler(event, data)
            except Exception:
                await session.rollback()
                deferred.discard()
                raise
            await session.commit()
        await deferred.flush()
        return result


class UserContextMiddleware(BaseMiddleware):
    """Регистрирует пользователя и передаёт обработчику user, turn и orchestrator."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        sender: TgUser | None = data.get("event_from_user")
        if sender is None or sender.is_bot:
            return await handler(event, data)

        session: AsyncSession = data["session"]
        access = AccessService(session, default_timezone=self._settings.default_timezone)
        user, created = await access.register_or_update_user(
            TelegramProfile(
                telegram_user_id=sender.id,
                username=sender.username,
                first_name=sender.first_name,
                last_name=sender.last_name,
                language_code=sender.language_code,
            )
        )
        project = await access.get_active_project(user)
        data["user"] = user
        data["user_created"] = created
        chat = data.get("event_chat")
        data["turn"] = Turn(
            user=user,
            project=project,
            telegram_message_id=event.message_id if isinstance(event, Message) else None,
            chat_id=chat.id if chat is not None else sender.id,
        )
        data["orchestrator"] = Orchestrator(
            session,
            llm=data["llm"],
            storage=data["file_storage"],
            settings=self._settings,
            jobs=data["jobs"],
        )
        return await handler(event, data)


class RateLimitMiddleware(BaseMiddleware):
    """Не больше N сообщений в минуту от пользователя. Предупреждаем один раз за окно."""

    def __init__(self, redis: Redis, *, per_minute: int) -> None:
        self._redis = redis
        self._limit = per_minute

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        sender: TgUser | None = data.get("event_from_user")
        if sender is None or not isinstance(event, Message):
            return await handler(event, data)
        key = f"rl:msg:{sender.id}:{int(time.time() // 60)}"
        count = int(await self._redis.incr(key))
        if count == 1:
            await self._redis.expire(key, 70)
        if count > self._limit:
            if count == self._limit + 1:
                await event.answer(texts.RATE_LIMITED)
            return None
        return await handler(event, data)
