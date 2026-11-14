"""Брокер фоновых задач Taskiq.

Redis Streams с подтверждением: если воркер упадёт посреди задачи, она будет доставлена
повторно (поэтому обработчики задач обязаны быть идемпотентными). В тестах — брокер в памяти.
Расписание хранится не в брокере, а в PostgreSQL; созревшие публикации ставит в очередь
диспетчер (python -m app.scheduler).
"""

from __future__ import annotations

from taskiq import AsyncBroker, InMemoryBroker, SmartRetryMiddleware, TaskiqEvents, TaskiqState
from taskiq_redis import RedisStreamBroker

from app.core.config import get_settings
from app.core.logging import configure_logging


def _create_broker() -> AsyncBroker:
    settings = get_settings()
    if settings.app_env == "test":
        return InMemoryBroker(await_inplace=True)
    broker = RedisStreamBroker(
        url=settings.redis_url, queue_name="ai-marketer", consumer_group_name="workers"
    )
    return broker.with_middlewares(
        SmartRetryMiddleware(default_retry_count=3, default_delay=5, use_jitter=True, use_delay_exponent=True)
    )


broker = _create_broker()


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def _on_worker_startup(state: TaskiqState) -> None:
    from app.workers.deps import build_worker_deps  # импорт здесь: зависимости нужны только воркеру

    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json)
    state.deps = await build_worker_deps(settings)


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def _on_worker_shutdown(state: TaskiqState) -> None:
    deps = getattr(state, "deps", None)
    if deps is not None:
        await deps.aclose()
