"""Запись расхода токенов.

Пишется в отдельной сессии с собственным commit: даже если обработчик потом упадёт
и откатит свою транзакцию, факт обращения к модели и потраченные токены сохранятся.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.models import LLMUsage
from app.domain.enums import LLMCallStatus

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class UsageRecord:
    purpose: str
    provider: str
    model: str
    status: LLMCallStatus
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    project_id: UUID | None = None
    user_id: UUID | None = None
    error: str | None = None


class UsageSink(Protocol):
    async def record(self, entry: UsageRecord) -> None: ...


class MemoryUsageSink:
    def __init__(self) -> None:
        self.records: list[UsageRecord] = []

    async def record(self, entry: UsageRecord) -> None:
        self.records.append(entry)


class SqlUsageSink:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(self, entry: UsageRecord) -> None:
        try:
            async with self._session_factory() as session:
                session.add(
                    LLMUsage(
                        project_id=entry.project_id,
                        user_id=entry.user_id,
                        purpose=entry.purpose,
                        provider=entry.provider,
                        model=entry.model,
                        input_tokens=entry.input_tokens,
                        output_tokens=entry.output_tokens,
                        latency_ms=entry.latency_ms,
                        status=entry.status,
                        error=entry.error,
                    )
                )
                await session.commit()
        except Exception:
            # Учёт не должен ломать ответ пользователю.
            log.exception("llm.usage_record_failed", purpose=entry.purpose)
