"""Хранилище файлов: локальная папка в MVP, S3-совместимое — позже, без изменений в сервисах."""

from __future__ import annotations

from typing import Protocol


class FileStorage(Protocol):
    backend: str

    async def save(self, key: str, data: bytes) -> None: ...

    async def read(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...
