"""Память агента: пожелания владельца, которые учитываются во всех следующих генерациях."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.business_analyst import normalize_for_match
from app.db.models import AgentMemory
from app.domain.enums import MemoryKind

MAX_PREFERENCES = 15


class MemoryService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_preference(self, project_id: UUID, content: str, *, source: str) -> bool:
        clean = " ".join(content.split())[:500]
        if not clean:
            return False
        existing = await self.preferences(project_id)
        if normalize_for_match(clean) in {normalize_for_match(p) for p in existing}:
            return False
        self._session.add(
            AgentMemory(project_id=project_id, kind=MemoryKind.PREFERENCE, content=clean, source=source)
        )
        await self._session.flush()
        return True

    async def preferences(self, project_id: UUID) -> list[str]:
        rows = await self._session.scalars(
            select(AgentMemory.content)
            .where(
                AgentMemory.project_id == project_id,
                AgentMemory.kind == MemoryKind.PREFERENCE,
                AgentMemory.active.is_(True),
            )
            .order_by(AgentMemory.created_at.desc())
            .limit(MAX_PREFERENCES)
        )
        return list(reversed(list(rows)))

    async def forget_all(self, project_id: UUID) -> int:
        result = await self._session.execute(
            update(AgentMemory)
            .where(AgentMemory.project_id == project_id, AgentMemory.active.is_(True))
            .values(active=False)
        )
        await self._session.flush()
        return int(getattr(result, "rowcount", 0) or 0)
