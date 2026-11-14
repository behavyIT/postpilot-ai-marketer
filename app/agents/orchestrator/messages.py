"""Тексты ответов оркестратора (HTML-разметка Telegram). Пользовательские данные экранируются."""

from __future__ import annotations

from html import escape

from app.agents.orchestrator.intents import Intent, IntentResult
from app.integrations.documents.detect import SUPPORTED_FORMATS_HINT

HELP = (
    "<b>Что я умею</b>\n\n"
    "Пишите обычными сообщениями — я пойму, что нужно сделать. Например:\n"
    "• «У нас кофейня в Казани, варим спешелти»\n"
    "• ссылка на сайт или файл: презентация, прайс, описание услуг\n"
    "• «Составь контент-план на неделю»\n"
    "• «Сделай пост о новой услуге»\n\n"
    "<b>Команды</b>\n"
    "/project — проекты (бизнесы)\n"
    "/profile — что я знаю о бизнесе\n"
    "/strategy — стратегия продвижения\n"
    "/plan — контент-план\n"
    "/content — публикация\n"
    "/schedule — расписание публикаций\n"
    "/analytics — аналитика\n"
    "/settings — настройки\n"
    "/pause и /resume — пауза и возобновление автоматической работы\n\n"
    "<b>Посты</b>\n"
    "Под каждым постом кнопки: ✅ одобрить, ✏️ изменить, 🖼 картинка, 📅 запланировать. "
    "Чтобы поправить пост, можно просто ответить на него: «короче и с юмором».\n"
    "Канал подключается так: сделайте бота администратором канала с правом публикации "
    "и перешлите мне любой пост оттуда.\n\n"
    "Ничего не публикую без вашего одобрения."
)

ASK_ABOUT_BUSINESS = (
    "Расскажите о своём бизнесе или отправьте ссылку на сайт. "
    f"Можно прислать и файлы: {SUPPORTED_FORMATS_HINT}."
)

SMALLTALK = "Я AI-маркетолог и помогаю с продвижением. " + ASK_ABOUT_BUSINESS + " Все возможности — /help."

UNKNOWN = (
    "Я помогаю с продвижением бизнеса: изучаю бизнес, готовлю стратегию, контент-планы и посты. "
    "Не понял, что сделать с этим сообщением, — уточните, пожалуйста, или загляните в /help."
)

ASK_PROJECT_NAME = "Как назвать новый проект? Обычно это название бизнеса, например «Кофейня Зерно»."

ASK_WEBSITE = "Пришлите ссылку на сайт — например, https://example.ru"

NO_PROJECT = "У вас пока нет проекта. " + ASK_ABOUT_BUSINESS

ANALYSIS_STARTED = "Изучаю — пришлю профиль бизнеса через 1–3 минуты. Можно присылать ещё материалы."

NOTE_SAVED_HINT = (
    "Обновлю профиль бизнеса и пришлю его через минуту. Ещё лучше, если пришлёте ссылку на сайт "
    "или документы: презентацию, прайс, описание услуг."
)

PROFILE_REFRESHING = "Обновляю профиль бизнеса — пришлю результат через минуту."

NEED_PROFILE = "Чтобы предлагать стратегию и посты, мне нужно узнать бизнес. " + ASK_ABOUT_BUSINESS

PROFILE_FIRST = (
    "Сначала изучу присланные материалы и составлю профиль бизнеса — пришлю через пару минут. "
    "После этого займусь стратегией и контентом."
)

STRATEGY_STARTED = "Готовлю стратегию продвижения — пришлю через минуту."

POSTS_STARTED = "Пишу тексты: постов — {count}. Буду присылать по мере готовности, это займёт пару минут."

POST_STARTED = "Пишу пост — пришлю через полминуты."

ASK_POST_TOPIC = "О чём написать пост? Например: «пост про осенний раф, акцент на пряностях»."

NO_PLAN = "Контент-плана пока нет. Составить план на неделю?"

MEMORY_FORGOTTEN = (
    "Забыл все ваши пожелания по контенту. Дальше буду опираться только на профиль и стратегию."
)

PREFERENCE_HINT = (
    "Чтобы я что-то запомнил, напишите, например: «запомни: пиши без эмодзи» или «хочу посты чаще»."
)


def preference_saved(text: str, *, duplicate: bool) -> str:
    head = "Это я уже помню" if duplicate else "Запомнил"
    return (
        f"{head}: «{q(text)}». Учту в стратегии, контент-планах и постах.\n\n"
        "Если пожелание меняет подход (например, частоту публикаций), обновите стратегию."
    )


def plan_started(days: int) -> str:
    return f"Составляю контент-план на {period_phrase(days)} — пришлю через 1–2 минуты."


COMING_SOON_SUFFIX = (
    "\n\nЭто я научусь делать в ближайших обновлениях. Начнём с изучения бизнеса: "
    "расскажите о нём, пришлите сайт или документы — на их основе я подготовлю стратегию и контент."
)

STATUS_LABELS = {"active": "работает", "paused": "на паузе", "archived": "в архиве"}


def q(value: str) -> str:
    """Экранировать пользовательский текст для HTML."""
    return escape(value, quote=False)


def period_phrase(days: int | None) -> str:
    return {7: "неделю", 14: "две недели", 30: "месяц"}.get(days or 0, f"{days} дн." if days else "неделю")


def coming_soon(result: IntentResult) -> str:
    """Показать, что запрос понят, и честно сказать, что функции пока нет."""
    intent = result.intent
    if intent is Intent.CREATE_STRATEGY:
        head = "Понял: нужна стратегия продвижения."
    elif intent is Intent.CREATE_PLAN:
        head = f"Понял: нужен контент-план на {period_phrase(result.period_days)}."
    elif intent is Intent.CREATE_POST:
        head = "Понял: нужен пост" + (f" на тему «{q(result.topic)}»" if result.topic else "") + "."
        if result.instructions:
            head += f" Пожелания: {q(result.instructions)}."
    elif intent is Intent.EDIT_POST:
        head = "Понял, что поправить" + (f": {q(result.instructions)}." if result.instructions else ".")
    elif intent is Intent.GENERATE_IMAGE:
        head = "Понял: нужна картинка" + (f" — {q(result.topic)}" if result.topic else "") + "."
    elif intent is Intent.SCHEDULE_POST:
        head = "Понял: запланировать публикацию" + (f" — {q(result.when)}" if result.when else "") + "."
    elif intent is Intent.SHOW_SCHEDULE:
        head = "Понял: показать расписание публикаций."
    elif intent is Intent.CANCEL_POST:
        head = "Понял: отменить запланированную публикацию."
    elif intent is Intent.ANALYTICS:
        period = f" за {period_phrase(result.period_days)}" if result.period_days else ""
        head = f"Понял: нужен отчёт по статистике публикаций{period}."
    elif intent is Intent.FIND_TOPICS:
        head = "Понял: подобрать темы, интересные вашей аудитории."
    else:
        head = "Понял."
    return head + COMING_SOON_SUFFIX
