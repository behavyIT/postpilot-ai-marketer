"""Карточки постов в чате: превью с кнопками по статусу, выбор времени, итог публикации."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.agents.orchestrator.content_view import when_label
from app.agents.orchestrator.messages import q
from app.agents.orchestrator.replies import Action, Button, Reply
from app.db.models import SocialAccount
from app.domain.enums import ContentGoal, PublicationStatus
from app.domain.publications.refs import PostRef
from app.domain.publications.state_machine import LABELS
from app.schemas.content import GOAL_LABELS
from app.services.publications import PostView
from app.services.publishing import OutcomeKind, PublishOutcome

S = PublicationStatus
LIMIT = 3900
RULE = "———"

CHANNEL_HOWTO = (
    "<b>Как подключить Telegram-канал</b>\n"
    "1. Откройте канал → Управление каналом → Администраторы → Добавить администратора.\n"
    "2. Найдите этого бота и включите право «Публикация сообщений».\n"
    "3. Перешлите мне любой пост из канала или пришлите его адрес, например @my_channel.\n\n"
    "Подключить канал может только его администратор."
)

# Варианты времени для кнопок: код → (подпись, вычисление).
_QUICK_TIMES = {
    "1h": "Через час",
    "t19": "Сегодня 19:00",
    "m10": "Завтра 10:00",
    "m19": "Завтра 19:00",
}


@dataclass(frozen=True, slots=True)
class ImageData:
    data: bytes
    name: str


def _ref(view: PostView) -> PostRef:
    return PostRef(view.variant.id, view.version.version_no)


def _goal(value: str) -> str:
    try:
        return GOAL_LABELS[ContentGoal(value)]
    except ValueError:
        return value


def _fit(text: str) -> str:
    return text if len(text) <= LIMIT else text[: LIMIT - 1].rstrip() + "…"


def post_card(
    view: PostView,
    *,
    timezone: str,
    image: ImageData | None,
    head: str | None = None,
) -> Reply:
    """Превью поста для владельца: служебная шапка, текст ровно как в канале, предупреждения, кнопки."""
    variant, version, publication = view.variant, view.version, view.publication
    status = S(variant.status)
    meta = [when_label(publication.planned_at, timezone)] if publication.planned_at else []
    meta += [q(publication.category), f"цель: {_goal(publication.goal)}"]
    lines = [
        head or f"<b>Пост «{q(publication.title)}»</b>",
        " · ".join(meta),
        f"Статус: {LABELS[status]} · версия {version.version_no}",
    ]
    if status == S.SCHEDULED and variant.scheduled_at:
        channel = f" в «{q(view.account.title)}»" if view.account else ""
        lines.append(f"📅 Выйдет: {when_label(variant.scheduled_at, timezone)}{channel}")
    if status == S.PUBLISHED and variant.published_at:
        lines.append(f"📢 Опубликован: {when_label(variant.published_at, timezone)}")
    if status == S.FAILED and variant.failure_reason:
        lines.append(f"⚠️ Причина: {q(variant.failure_reason)}")
    lines += [RULE, version.body, RULE]
    if version.warnings:
        lines += ["⚠️ <b>Проверьте перед публикацией:</b>", *(f"• {q(w)}" for w in version.warnings)]
    if image is None and publication.visual_idea and status in _EDITABLE_VIEW:
        lines.append(f"🖼 <i>Идея картинки: {q(publication.visual_idea)}</i>")
    if status in {S.DRAFT, S.PENDING_APPROVAL}:
        lines.append("Ответьте на это сообщение, чтобы попросить правку, — или нажмите кнопку.")
    return Reply(
        text=_fit("\n".join(lines)),
        buttons=_buttons(view, has_image=image is not None),
        image=image.data if image else None,
        image_name=image.name if image else "image.jpg",
        post_ref=_ref(view),
        intent="post_card",
    )


_EDITABLE_VIEW = {S.DRAFT, S.PENDING_APPROVAL, S.NEEDS_REVISION, S.APPROVED, S.REJECTED}


def _buttons(view: PostView, *, has_image: bool) -> list[list[Button]]:
    value = _ref(view).value()
    status = S(view.variant.status)
    image_button = Button(
        text="🖼 Другая картинка" if has_image else "🖼 Картинка", action=Action.POST_IMAGE, value=value
    )
    if status in {S.DRAFT, S.PENDING_APPROVAL}:
        rows = [
            [
                Button(text="✅ Одобрить", action=Action.POST_APPROVE, value=value),
                Button(text="✏️ Изменить", action=Action.POST_EDIT, value=value),
            ],
            [Button(text="🔄 Другой вариант", action=Action.POST_REDO, value=value), image_button],
            [
                Button(text="👁 Как в канале", action=Action.POST_PREVIEW, value=value),
                Button(text="❌ Отклонить", action=Action.POST_REJECT, value=value),
            ],
        ]
        if has_image:
            rows.append([Button(text="🗑 Без картинки", action=Action.POST_NO_IMAGE, value=value)])
        return rows
    if status == S.NEEDS_REVISION:
        return [
            [
                Button(text="🔄 Другой вариант", action=Action.POST_REDO, value=value),
                Button(text="✏️ Изменить", action=Action.POST_EDIT, value=value),
            ],
            [Button(text="↩️ Оставить как было", action=Action.POST_KEEP, value=value)],
        ]
    if status == S.REJECTED:
        return [[Button(text="🔄 Переписать", action=Action.POST_REDO, value=value)]]
    if status == S.APPROVED:
        return [
            [
                Button(text="📅 Запланировать", action=Action.POST_WHEN, value=value),
                Button(text="🚀 Опубликовать сейчас", action=Action.POST_NOW, value=value),
            ],
            [
                Button(text="✏️ Изменить", action=Action.POST_EDIT, value=value),
                Button(text="👁 Как в канале", action=Action.POST_PREVIEW, value=value),
            ],
        ]
    if status == S.SCHEDULED:
        return [
            [
                Button(text="🚀 Опубликовать сейчас", action=Action.POST_NOW, value=value),
                Button(text="⏹ Снять с расписания", action=Action.POST_UNSCHEDULE, value=value),
            ],
            [
                Button(text="🕒 Другое время", action=Action.POST_WHEN, value=value),
                Button(text="✏️ Изменить", action=Action.POST_EDIT, value=value),
            ],
        ]
    if status == S.FAILED:
        return [
            [
                Button(text="🔁 Повторить", action=Action.POST_RETRY, value=value),
                Button(text="✅ Пост вышел", action=Action.POST_DONE, value=value),
            ],
            [Button(text="🚫 Отменить пост", action=Action.POST_CANCEL, value=value)],
        ]
    if status == S.PUBLISHED and view.variant.external_url:
        return [[Button(text="🔗 Открыть в канале", url=view.variant.external_url)]]
    return []


def channel_preview(view: PostView, image: ImageData | None) -> Reply:
    """Пост ровно так, как его увидят подписчики, — без служебных строк и кнопок."""
    return Reply(
        text=view.version.body,
        image=image.data if image else None,
        image_name=image.name if image else "image.jpg",
        intent="post_preview",
    )


def schedule_options(view: PostView, *, timezone: str, now: datetime) -> list[list[Button]]:
    value = _ref(view).value()
    local_now = now.astimezone(ZoneInfo(timezone))
    rows: list[list[Button]] = []
    planned = view.publication.planned_at
    if planned is not None and planned > now + timedelta(minutes=1):
        rows.append(
            [
                Button(
                    text=f"📌 По плану: {when_label(planned, timezone)}",
                    action=Action.POST_AT,
                    value=f"{value}.plan",
                )
            ]
        )
    quick = [code for code in _QUICK_TIMES if quick_time(code, local_now) is not None]
    for pair in (quick[:2], quick[2:4]):
        if pair:
            rows.append(
                [Button(text=_QUICK_TIMES[c], action=Action.POST_AT, value=f"{value}.{c}") for c in pair]
            )
    rows.append(
        [
            Button(text="✍️ Своё время", action=Action.POST_AT, value=f"{value}.custom"),
            Button(text="🚀 Сейчас", action=Action.POST_NOW, value=value),
        ]
    )
    return rows


def quick_time(code: str, local_now: datetime) -> datetime | None:
    """Момент для кнопки быстрого выбора (в часовом поясе проекта) или None, если вариант неуместен."""
    tomorrow = local_now.date() + timedelta(days=1)
    if code == "1h":
        return local_now + timedelta(hours=1)
    if code == "t19":
        today_19 = datetime.combine(local_now.date(), time(19, 0), tzinfo=local_now.tzinfo)
        return today_19 if today_19 > local_now + timedelta(minutes=30) else None
    if code == "m10":
        return datetime.combine(tomorrow, time(10, 0), tzinfo=local_now.tzinfo)
    if code == "m19":
        return datetime.combine(tomorrow, time(19, 0), tzinfo=local_now.tzinfo)
    return None


def approved_reply(view: PostView, *, timezone: str, account: SocialAccount | None, now: datetime) -> Reply:
    head = f"✅ Одобрено: «{q(view.publication.title)}» (версия {view.version.version_no})."
    if account is None:
        return Reply(
            text=f"{head}\n\nЧтобы публиковать, подключите канал.\n\n{CHANNEL_HOWTO}",
            buttons=[[Button(text="👁 Показать пост", action=Action.POST_OPEN, value=view.variant.id.hex)]],
            close_source=True,
            intent="post_approved",
        )
    return Reply(
        text=f"{head}\nКогда публикуем в «{q(account.title)}»?",
        buttons=schedule_options(view, timezone=timezone, now=now),
        close_source=True,
        intent="post_approved",
    )


def scheduled_reply(view: PostView, *, timezone: str, paused: bool) -> Reply:
    at = view.variant.scheduled_at
    channel = f" в «{q(view.account.title)}»" if view.account else ""
    value = _ref(view).value()
    text = f"📅 «{q(view.publication.title)}» выйдет {when_label(at, timezone)}{channel}."
    if paused:
        text += "\n\n⏸ Проект на паузе: пока не нажмёте /resume, пост не выйдет."
    return Reply(
        text=text,
        buttons=[
            [
                Button(text="⏹ Снять с расписания", action=Action.POST_UNSCHEDULE, value=value),
                Button(text="🕒 Другое время", action=Action.POST_WHEN, value=value),
            ]
        ],
        close_source=True,
        intent="post_scheduled",
    )


def outcome_reply(outcome: PublishOutcome) -> Reply | None:
    """Сообщение владельцу об итоге публикации; None — сообщать нечего."""
    title = q(outcome.title)
    value_open = outcome.variant_id.hex
    if outcome.kind is OutcomeKind.PUBLISHED:
        channel = f" в «{q(outcome.channel_title)}»" if outcome.channel_title else ""
        buttons = [[Button(text="🔗 Открыть пост", url=outcome.url)]] if outcome.url else []
        return Reply(text=f"📢 Опубликовано{channel}: «{title}».", buttons=buttons, intent="post_published")
    if outcome.kind is OutcomeKind.UNKNOWN:
        return Reply(
            text=(
                f"❓ Не удалось подтвердить публикацию «{title}»: {q(outcome.reason)}.\n"
                "Проверьте канал: если пост вышел — нажмите «Пост вышел», если нет — «Повторить». "
                "Сам повторять не стал, чтобы не получилось дубля."
            ),
            buttons=[[Button(text="👁 Открыть пост", action=Action.POST_OPEN, value=value_open)]],
            intent="post_unknown",
        )
    if outcome.kind is OutcomeKind.FAILED:
        return Reply(
            text=f"⚠️ Не опубликовал «{title}»: {q(outcome.reason)}.",
            buttons=[[Button(text="👁 Открыть пост", action=Action.POST_OPEN, value=value_open)]],
            intent="post_failed",
        )
    if outcome.kind is OutcomeKind.BLOCKED:
        return Reply(
            text=(
                f"⛔ «{title}» не опубликован: {q(outcome.reason)}. "
                "Пост вернулся на доработку — посмотрите и одобрите его заново."
            ),
            buttons=[[Button(text="👁 Открыть пост", action=Action.POST_OPEN, value=value_open)]],
            intent="post_blocked",
        )
    return None


def overview_reply(
    project_name: str,
    *,
    timezone: str,
    account: SocialAccount | None,
    scheduled: list[PostView],
    published: list[PostView],
    counts: dict[str, int],
    paused: bool,
) -> Reply:
    lines = [f"<b>Публикации «{q(project_name)}»</b>"]
    if account is None:
        lines.append("Канал: не подключён")
    elif account.can_post:
        lines.append(f"Канал: «{q(account.title)}» ✅")
    else:
        lines.append(f"Канал: «{q(account.title)}» — ⚠️ у бота нет права публиковать")
    if paused:
        lines.append("⏸ Проект на паузе — по расписанию ничего не выходит")
    lines.append("")
    if scheduled:
        lines.append(f"<b>📅 Запланировано ({len(scheduled)})</b>")
        lines += [
            f"• {when_label(v.variant.scheduled_at, timezone)} — «{q(v.publication.title)}»"
            for v in scheduled
        ]
    else:
        lines.append("📅 Запланированных публикаций нет")
    if published:
        lines += ["", "<b>📢 Опубликовано недавно</b>"]
        for v in published:
            title = q(v.publication.title)
            link = (
                f'<a href="{q(v.variant.external_url)}">«{title}»</a>'
                if v.variant.external_url
                else f"«{title}»"
            )
            lines.append(f"• {when_label(v.variant.published_at, timezone)} — {link}")

    buttons: list[list[Button]] = []
    labels = {
        "pending": "⏳ Ждут одобрения",
        "approved": "✅ Одобрены, без времени",
        "scheduled": "📅 Запланированные",
        "failed": "⚠️ Не вышли",
    }
    for group, label in labels.items():
        if counts.get(group):
            buttons.append([Button(text=f"{label} ({counts[group]})", action=Action.POST_LIST, value=group)])
    if account is None or not account.can_post:
        buttons.append([Button(text="📢 Как подключить канал", action=Action.CHANNEL_HELP)])
    buttons.append([Button(text="📋 Контент-план", action=Action.PLAN_SHOW)])
    return Reply(text=_fit("\n".join(lines)), buttons=buttons, intent="show_schedule")
