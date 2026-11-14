"""Постановка фоновых задач из сценариев (бот, API).

Задачи отправляются в очередь только после фиксации транзакции: иначе воркер мог бы
начать работу раньше, чем в базе появятся данные, которые он должен обработать.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ProfileAnalysisJob:
    project_id: UUID
    user_id: UUID
    chat_id: int | None
    force: bool = False


class ContentJobKind(StrEnum):
    STRATEGY = "strategy"
    PLAN = "plan"
    PLAN_POSTS = "plan_posts"
    SINGLE_POST = "single_post"


@dataclass(frozen=True, slots=True)
class ContentJob:
    kind: ContentJobKind
    project_id: UUID
    user_id: UUID
    chat_id: int | None
    days: int | None = None
    instructions: str | None = None
    plan_id: UUID | None = None
    topic: str | None = None


Job = ProfileAnalysisJob | ContentJob


class JobQueue(Protocol):
    async def enqueue(self, job: Job) -> None: ...


class MemoryJobQueue:
    """Для тестов: запоминает поставленные задачи."""

    def __init__(self) -> None:
        self.jobs: list[Job] = []

    async def enqueue(self, job: Job) -> None:
        self.jobs.append(job)

    @property
    def profile_jobs(self) -> list[ProfileAnalysisJob]:
        return [j for j in self.jobs if isinstance(j, ProfileAnalysisJob)]

    @property
    def content_jobs(self) -> list[ContentJob]:
        return [j for j in self.jobs if isinstance(j, ContentJob)]


class DeferredJobQueue:
    """Копит задачи во время обработки сообщения и отправляет их после commit."""

    def __init__(self, inner: JobQueue) -> None:
        self._inner = inner
        self._pending: list[Job] = []

    async def enqueue(self, job: Job) -> None:
        if job not in self._pending:
            self._pending.append(job)

    async def flush(self) -> None:
        pending, self._pending = self._pending, []
        for job in pending:
            await self._inner.enqueue(job)

    def discard(self) -> None:
        self._pending.clear()
