"""Постановка задач в Taskiq с защитой от дублей."""

from __future__ import annotations

from redis.asyncio import Redis

from app.services.jobs import ProfileAnalysisJob
from app.workers.tasks.profile import analyze_profile, queued_key


class TaskiqJobQueue:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def enqueue_profile_analysis(self, job: ProfileAnalysisJob) -> None:
        # Пока задача по проекту ждёт старта, новые не ставим: она и так увидит все новые источники.
        if await self._redis.set(queued_key(job.project_id), "1", nx=True, ex=900):
            await analyze_profile.kiq(str(job.project_id), str(job.user_id), job.chat_id, job.force)
