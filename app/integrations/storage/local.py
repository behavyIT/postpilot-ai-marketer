"""Хранилище файлов в локальной папке."""

from __future__ import annotations

import asyncio
from pathlib import Path


class LocalFileStorage:
    backend = "local"

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def _path(self, key: str) -> Path:
        path = (self._root / key).resolve()
        # Ключи формирует приложение, но проверяем, что путь не выходит за пределы корня.
        if not path.is_relative_to(self._root):
            raise ValueError(f"Недопустимый ключ файла: {key}")
        return path

    async def save(self, key: str, data: bytes) -> None:
        path = self._path(key)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)  # атомарно: читатель не увидит недописанный файл

        await asyncio.to_thread(_write)

    async def read(self, key: str) -> bytes:
        return await asyncio.to_thread(self._path(key).read_bytes)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._path(key).unlink, missing_ok=True)
