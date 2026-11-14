"""Перечисления предметной области.

В БД они хранятся как text + CHECK-ограничение: такие значения проще мигрировать,
чем типы ENUM в PostgreSQL.
"""

from __future__ import annotations

from enum import StrEnum


class UserStatus(StrEnum):
    ACTIVE = "active"
    BLOCKED = "blocked"


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class AutomationMode(StrEnum):
    OFF = "off"
    ASSISTANT = "assistant"


class ProjectRole(StrEnum):
    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


class ActorType(StrEnum):
    USER = "user"
    SYSTEM = "system"
    AI = "ai"


def check_values(enum_cls: type[StrEnum]) -> str:
    """SQL-список значений для CHECK-ограничения: 'a', 'b', 'c'."""
    return ", ".join(f"'{member.value}'" for member in enum_cls)
