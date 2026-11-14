"""Служебные задачи."""

from __future__ import annotations

from app.core.logging import get_logger
from app.workers.broker import broker

log = get_logger(__name__)


@broker.task(task_name="system.ping")
async def ping(payload: str = "ping") -> str:
    """Проверка, что очередь и воркер работают: возвращает ответ на полученную строку."""
    log.info("system.ping", payload=payload)
    return f"pong: {payload}"
