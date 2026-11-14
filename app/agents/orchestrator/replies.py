"""Ответ оркестратора, независимый от Telegram.

Слой бота превращает Reply в сообщение с inline-кнопками; оркестратор ничего
не знает об aiogram и может обслуживать другой интерфейс (веб, MAX).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.domain.publications.refs import PostRef


class Expect(StrEnum):
    """Чего бот ждёт следующим сообщением."""

    PROJECT_NAME = "project_name"
    POST_EDIT = "post_edit"  # что изменить в посте (expect_data: ref)
    POST_TIME = "post_time"  # когда опубликовать (expect_data: ref)


class Action(StrEnum):
    """Действия inline-кнопок."""

    PROJECT_SWITCH = "project.switch"
    PROJECT_NEW = "project.new"
    PROJECT_LIST = "project.list"
    PROFILE_REFRESH = "profile.refresh"
    PROFILE_SHOW = "profile.show"
    STRATEGY_APPROVE = "strategy.approve"
    STRATEGY_REDO = "strategy.redo"
    PLAN_CREATE = "plan.create"
    PLAN_APPROVE = "plan.approve"
    PLAN_REDO = "plan.redo"
    PLAN_WRITE = "plan.write"
    PLAN_SHOW = "plan.show"
    MEMORY_FORGET = "memory.forget"
    HELP = "help"
    # Посты. Значение кнопки — «<variant_id hex>.<версия>»: старая кнопка не подействует на новую версию.
    POST_APPROVE = "post.ok"
    POST_EDIT = "post.edit"
    POST_REDO = "post.redo"
    POST_KEEP = "post.keep"
    POST_REJECT = "post.reject"
    POST_IMAGE = "post.image"
    POST_NO_IMAGE = "post.noimg"
    POST_PREVIEW = "post.view"
    POST_WHEN = "post.when"
    POST_AT = "post.at"  # значение: «<hex>.<версия>.<вариант времени>»
    POST_NOW = "post.now"
    POST_UNSCHEDULE = "post.unsched"
    POST_RETRY = "post.retry"
    POST_DONE = "post.done"
    POST_CANCEL = "post.cancel"
    POST_OPEN = "post.open"  # значение: «<hex>»
    POST_LIST = "post.list"  # значение: группа (pending, approved, scheduled, failed)
    CHANNEL_HELP = "channel.help"


@dataclass(frozen=True, slots=True)
class Button:
    text: str
    action: Action = Action.HELP
    value: str = ""
    url: str | None = None  # кнопка-ссылка (например, на опубликованный пост)


@dataclass(slots=True)
class Reply:
    text: str
    buttons: list[list[Button]] = field(default_factory=list)
    expect: Expect | None = None
    intent: str | None = None
    expect_data: dict[str, str] = field(default_factory=dict)
    image: bytes | None = None  # картинка поста: сообщение уходит фото с подписью
    image_name: str = "image.jpg"
    post_ref: PostRef | None = None  # превью поста: ответ на сообщение — правка этой версии
    close_source: bool = False  # убрать кнопки у сообщения, на котором нажали кнопку
    followups: list[Reply] = field(default_factory=list)  # ещё сообщения следом (например, превью постов)
