"""Тексты интерфейса бота (HTML-разметка Telegram). Остальные ответы — в оркестраторе."""

from __future__ import annotations

from html import escape

from app.agents.orchestrator.messages import ASK_ABOUT_BUSINESS

WELCOME_NEW = (
    "Привет{name}! Я твой AI-маркетолог.\n\n"
    "Помогу изучить бизнес, разработать стратегию, подготовить публикации "
    "и управлять социальными сетями. Ничего не публикую без твоего одобрения.\n\n"
    "Для начала расскажи о своём бизнесе или отправь ссылку на сайт."
)

WELCOME_BACK = "С возвращением{name}! Активный проект: <b>{project}</b>.\n\nЧем займёмся?"

WELCOME_BACK_NO_PROJECT = "С возвращением{name}! " + ASK_ABOUT_BUSINESS

CANCELLED = "Хорошо, отменил. Чем ещё помочь?"

UNSUPPORTED_MESSAGE = "Я понимаю текст, ссылки и файлы. Голосовые, стикеры и видео пока не разбираю."

FILE_TOO_LARGE = "Файл больше {limit} МБ — Telegram не даёт ботам скачивать такие. Пришлите файл поменьше."

RATE_LIMITED = "Слишком много сообщений подряд — дайте мне минуту, и продолжим."

INTERNAL_ERROR = "Что-то пошло не так. Попробуйте ещё раз — а если не получится, напишите /start."


def greeting_name(first_name: str | None) -> str:
    return f", {escape(first_name)}" if first_name else ""


def welcome_back(first_name: str | None, project_name: str | None) -> str:
    name = greeting_name(first_name)
    if project_name is None:
        return WELCOME_BACK_NO_PROJECT.format(name=name)
    return WELCOME_BACK.format(name=name, project=escape(project_name))
