"""Тексты интерфейса бота (HTML-разметка Telegram)."""

from __future__ import annotations

from html import escape

WELCOME_NEW = (
    "Привет{name}! Я твой AI-маркетолог.\n\n"
    "Помогу изучить бизнес, разработать стратегию, подготовить публикации "
    "и управлять социальными сетями. Ничего не публикую без твоего одобрения.\n\n"
    "Для начала расскажи о своём бизнесе или отправь ссылку на сайт."
)

WELCOME_BACK = "С возвращением{name}! Активный проект: <b>{project}</b>.\n\nЧем займёмся?"

WELCOME_BACK_NO_PROJECT = (
    "С возвращением{name}! Расскажи о своём бизнесе или отправь ссылку на сайт — с этого начнём."
)

HELP = (
    "<b>Что я умею</b>\n\n"
    "/start — начало работы\n"
    "/help — эта подсказка\n\n"
    "Скоро здесь появятся проекты, профиль бизнеса, стратегия, контент-план, "
    "публикации с согласованием, расписание и аналитика. "
    "Писать можно и обычными сообщениями — я пойму, что нужно сделать."
)

NOT_READY_YET = (
    "Понял тебя. Разбирать свободные сообщения я научусь на следующих этапах — "
    "пока работают команды /start и /help."
)


def greeting_name(first_name: str | None) -> str:
    return f", {escape(first_name)}" if first_name else ""


def welcome_back(first_name: str | None, project_name: str | None) -> str:
    name = greeting_name(first_name)
    if project_name is None:
        return WELCOME_BACK_NO_PROJECT.format(name=name)
    return WELCOME_BACK.format(name=name, project=escape(project_name))
