"""Ответ оркестратора, независимый от Telegram.

Слой бота превращает Reply в сообщение с inline-кнопками; оркестратор ничего
не знает об aiogram и может обслуживать другой интерфейс (веб, MAX).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Expect(StrEnum):
    """Чего бот ждёт следующим сообщением."""

    PROJECT_NAME = "project_name"


class Action(StrEnum):
    """Действия inline-кнопок."""

    PROJECT_SWITCH = "project.switch"
    PROJECT_NEW = "project.new"
    PROJECT_LIST = "project.list"
    HELP = "help"


@dataclass(frozen=True, slots=True)
class Button:
    text: str
    action: Action
    value: str = ""


@dataclass(slots=True)
class Reply:
    text: str
    buttons: list[list[Button]] = field(default_factory=list)
    expect: Expect | None = None
    intent: str | None = None
