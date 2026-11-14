"""Журнал переписки: сохранение реплик и выдача последних как контекста."""

from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ConversationMessage, User
from app.domain.enums import MessageRole

_TAG_RE = re.compile(r"<[^>]+>")
_MAX_STORED_CHARS = 4000


class ConversationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        user: User,
        role: MessageRole,
        content: str,
        *,
        project_id: UUID | None,
        intent: str | None = None,
        telegram_message_id: int | None = None,
    ) -> None:
        plain = _TAG_RE.sub("", content).strip()[:_MAX_STORED_CHARS]
        if not plain:
            return
        self._session.add(
            ConversationMessage(
                user_id=user.id,
                project_id=project_id,
                role=role,
                content=plain,
                intent=intent,
                telegram_message_id=telegram_message_id,
            )
        )
        await self._session.flush()

    async def recent(self, user: User, *, limit: int = 6) -> list[ConversationMessage]:
        """Последние реплики в хронологическом порядке."""
        rows = await self._session.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.user_id == user.id)
            .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
            .limit(limit)
        )
        return list(reversed(list(rows)))
