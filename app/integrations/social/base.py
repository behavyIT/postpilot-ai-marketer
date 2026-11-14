"""Единый интерфейс публикатора (§2.9 ТЗ): authenticate, validate_content, publish,
get_post, delete_post, get_statistics.

Чего площадка не умеет, публикатор честно сообщает через NotSupportedError, а не
возвращает выдуманный результат.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from app.domain.enums import Platform


class PublishErrorKind(StrEnum):
    RETRYABLE = "retryable"  # точно не опубликовано (нет соединения, 429) — можно повторить
    PERMANENT = "permanent"  # площадка отказала (нет прав, неверный формат) — повтор не поможет
    UNKNOWN = "unknown"  # запрос ушёл, ответа нет — пост мог выйти; автоматически не повторяем


class PublishError(Exception):
    def __init__(
        self, kind: PublishErrorKind, code: str, message: str, *, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.message = message
        self.retry_after = retry_after


class NotSupportedError(Exception):
    """Площадка или её API не дают такой возможности."""


@dataclass(frozen=True, slots=True)
class PostContent:
    text: str  # ровно то, что одобрил владелец (с хештегами и разметкой площадки)
    image: bytes | None = None
    image_name: str = "image.jpg"


@dataclass(frozen=True, slots=True)
class PublishResult:
    external_id: str
    url: str | None = None


@dataclass(frozen=True, slots=True)
class ChannelCheck:
    """Результат проверки канала: можно ли туда публиковать."""

    chat_id: int
    title: str
    username: str | None
    can_post: bool
    problems: list[str] = field(default_factory=list)
    capabilities: dict[str, Any] = field(default_factory=dict)


class Publisher(Protocol):
    platform: Platform

    async def authenticate(self) -> None: ...

    def validate_content(self, content: PostContent) -> list[str]: ...

    async def publish(self, target: str, content: PostContent) -> PublishResult: ...

    async def get_post(self, target: str, external_id: str) -> dict[str, Any]: ...

    async def delete_post(self, target: str, external_id: str) -> None: ...

    async def get_statistics(self, target: str, external_id: str) -> dict[str, Any]: ...
