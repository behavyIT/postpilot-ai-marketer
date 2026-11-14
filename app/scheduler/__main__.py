"""Запуск диспетчера расписания: python -m app.scheduler"""

from __future__ import annotations

import asyncio
import contextlib
import signal

from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import create_engine, create_session_factory
from app.scheduler.dispatcher import Dispatcher
from app.workers.broker import broker
from app.workers.queue import TaskiqJobQueue
from app.workers.tasks.publish import STALE_CHECK_KEY, resolve_stale

log = get_logger("app.scheduler")


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json)
    engine = create_engine(settings.database_url)
    redis = Redis.from_url(settings.redis_url)

    async def on_stale() -> None:
        if await redis.set(STALE_CHECK_KEY, "1", nx=True, ex=300):
            await resolve_stale.kiq()

    dispatcher = Dispatcher(create_session_factory(engine), TaskiqJobQueue(redis), on_stale=on_stale)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    await broker.startup()
    log.info("scheduler.started", interval_s=settings.scheduler_interval_s)
    try:
        while not stop.is_set():
            try:
                await dispatcher.tick()
            except Exception:
                log.exception(
                    "scheduler.tick_failed"
                )  # БД или Redis недоступны — попробуем на следующем шаге
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.scheduler_interval_s)
    finally:
        await broker.shutdown()
        await redis.aclose()
        await engine.dispose()
        log.info("scheduler.stopped")


if __name__ == "__main__":
    asyncio.run(main())
