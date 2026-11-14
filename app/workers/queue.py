"""Постановка задач в Taskiq с защитой от дублей."""

from __future__ import annotations

from redis.asyncio import Redis

from app.services.jobs import ContentJob, Job, ProfileAnalysisJob
from app.workers.tasks.content import content_queued_key, generate_content
from app.workers.tasks.profile import analyze_profile, queued_key


class TaskiqJobQueue:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def enqueue(self, job: Job) -> None:
        # Пока задача по проекту ждёт старта, новые не ставим: она и так увидит все новые источники.
        if isinstance(job, ProfileAnalysisJob) and await self._redis.set(
            queued_key(job.project_id), "1", nx=True, ex=900
        ):
            await analyze_profile.kiq(str(job.project_id), str(job.user_id), job.chat_id, job.force)
        # Повторное нажатие кнопки, пока задача того же типа не закончилась, ничего не запускает.
        elif isinstance(job, ContentJob) and await self._redis.set(
            content_queued_key(job.project_id, job.kind), "1", nx=True, ex=1800
        ):
            await generate_content.kiq(
                job.kind.value,
                str(job.project_id),
                str(job.user_id),
                job.chat_id,
                job.days,
                job.instructions,
                str(job.plan_id) if job.plan_id else None,
                job.topic,
            )
