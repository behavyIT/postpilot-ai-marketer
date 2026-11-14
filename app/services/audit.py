"""Запись событий в журнал аудита."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog
from app.domain.enums import ActorType


class AuditService:
    """Только добавляет записи: изменять или удалять их приложение не умеет."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        action: str,
        actor_type: ActorType,
        actor_id: UUID | None = None,
        project_id: UUID | None = None,
        entity_type: str | None = None,
        entity_id: UUID | None = None,
        data: dict[str, Any] | None = None,
    ) -> AuditLog:
        entry = AuditLog(
            action=action,
            actor_type=actor_type,
            actor_id=actor_id,
            project_id=project_id,
            entity_type=entity_type,
            entity_id=entity_id,
            data=data or {},
        )
        self._session.add(entry)
        await self._session.flush()
        return entry
