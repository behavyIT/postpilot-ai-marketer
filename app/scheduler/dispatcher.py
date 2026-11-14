"""Диспетчер: раз в N секунд находит в БД посты, время которых наступило, и ставит их в очередь.

Расписание живёт в PostgreSQL, а не в брокере: перезапуск Redis или воркера его не теряет.
Повторная постановка безопасна — ключ «уже в очереди» в Redis и проверки в БД
(блокировка строки, статус, одобрение) не дадут опубликовать пост дважды.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.models import PublicationVariant
from app.domain.enums import PublicationStatus
from app.services.jobs import JobQueue, PublishJob
from app.services.publishing import STALE_AFTER, due_variants

log = get_logger(__name__)


class Dispatcher:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        queue: JobQueue,
        *,
        on_stale: Callable[[], Awaitable[None]],
    ) -> None:
        self._sf = session_factory
        self._queue = queue
        self._on_stale = on_stale

    async def tick(self, *, now: datetime | None = None) -> int:
        """Один проход; возвращает число поставленных в очередь публикаций."""
        now = now or datetime.now(UTC)
        async with self._sf() as session:
            due = await due_variants(session, now=now)
            stale = await session.scalar(
                select(
                    exists().where(
                        PublicationVariant.status == PublicationStatus.PUBLISHING,
                        PublicationVariant.updated_at < now - STALE_AFTER,
                    )
                )
            )
        for variant_id, version_no in due:
            await self._queue.enqueue(PublishJob(variant_id, version_no))
        if due:
            log.info("dispatcher.enqueued", count=len(due))
        if stale:
            await self._on_stale()
        return len(due)
