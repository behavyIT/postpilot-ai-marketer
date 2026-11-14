"""Постановка фоновых задач из сценариев (бот, API).

Задачи отправляются в очередь только после фиксации транзакции: иначе воркер мог бы
начать работу раньше, чем в базе появятся данные, которые он должен обработать.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ProfileAnalysisJob:
    project_id: UUID
    user_id: UUID
    chat_id: int | None
    force: bool = False


class JobQueue(Protocol):
    async def enqueue_profile_analysis(self, job: ProfileAnalysisJob) -> None: ...


class MemoryJobQueue:
    """Для тестов: запоминает поставленные задачи."""

    def __init__(self) -> None:
        self.profile_jobs: list[ProfileAnalysisJob] = []

    async def enqueue_profile_analysis(self, job: ProfileAnalysisJob) -> None:
        self.profile_jobs.append(job)


class DeferredJobQueue:
    """Копит задачи во время обработки сообщения и отправляет их после commit."""

    def __init__(self, inner: JobQueue) -> None:
        self._inner = inner
        self._pending: list[ProfileAnalysisJob] = []

    async def enqueue_profile_analysis(self, job: ProfileAnalysisJob) -> None:
        if job not in self._pending:
            self._pending.append(job)

    async def flush(self) -> None:
        pending, self._pending = self._pending, []
        for job in pending:
            await self._inner.enqueue_profile_analysis(job)

    def discard(self) -> None:
        self._pending.clear()
