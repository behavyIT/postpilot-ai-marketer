"""Ограничение одновременных запросов к модели.

На бесплатном тарифе GigaChat физлицу доступен один поток. Бот и воркер — разные
процессы, поэтому очередь общая, через блокировку в Redis.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import LockError

from app.ai.llm.base import LLMBusyError


class ConcurrencyLimiter(Protocol):
    def slot(self) -> AbstractAsyncContextManager[None]: ...


class NullLimiter:
    """Без ограничений (тесты, провайдеры без лимита потоков)."""

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        yield


class RedisLimiter:
    """Распределённая блокировка: один запрос к провайдеру во всех процессах сразу.

    ttl_s — страховка: если процесс упадёт, блокировка освободится сама.
    wait_s — сколько ждать своей очереди, прежде чем сообщить пользователю о занятости.
    """

    def __init__(self, redis: Redis, key: str, *, ttl_s: float, wait_s: float) -> None:
        self._redis = redis
        self._key = key
        self._ttl_s = ttl_s
        self._wait_s = wait_s

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        lock = self._redis.lock(self._key, timeout=self._ttl_s, blocking_timeout=self._wait_s)
        acquired = await lock.acquire()
        if not acquired:
            raise LLMBusyError("Модель сейчас занята другими задачами — попробуйте через минуту")
        try:
            yield
        finally:
            # Если блокировка истекла по ttl, пока шёл запрос, освобождать уже нечего.
            with suppress(LockError):
                await lock.release()
