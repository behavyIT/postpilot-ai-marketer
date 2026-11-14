"""Представление стратегии, контент-плана и черновиков постов в чате."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.agents.orchestrator.messages import q
from app.agents.orchestrator.replies import Action, Button, Reply
from app.db.models import ContentPlan, Publication
from app.domain.enums import ContentGoal, DocumentStatus
from app.schemas.content import GOAL_LABELS, StrategyData

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
STATUS = {
    DocumentStatus.DRAFT: "черновик",
    DocumentStatus.APPROVED: "утверждена",
    DocumentStatus.ARCHIVED: "архив",
}
PLAN_STATUS = {
    DocumentStatus.DRAFT: "черновик",
    DocumentStatus.APPROVED: "утверждён",
    DocumentStatus.ARCHIVED: "архив",
}
LIMIT = 3900
_PLATFORM_TITLES = {"telegram": "Telegram", "vk": "ВКонтакте", "max": "MAX"}


def _goal(value: str) -> str:
    try:
        return GOAL_LABELS[ContentGoal(value)]
    except ValueError:
        return value


def _fit(lines: list[str]) -> str:
    text = "\n".join(lines)
    return text if len(text) <= LIMIT else text[: LIMIT - 1].rstrip() + "…"


def strategy_reply(
    project_name: str, data: StrategyData, *, strategy_id: str, version: int, status: str
) -> Reply:
    lines = [
        f"<b>Стратегия продвижения «{q(project_name)}»</b>",
        f"вариант {version} · {STATUS.get(DocumentStatus(status), status)}",
        "",
        q(data.summary),
        "",
        f"<b>Позиционирование.</b> {q(data.positioning)}",
        "",
        "<b>Аудитория</b>",
    ]
    lines += [f"• {q(s.name)}: {q(s.needs)} → {q(s.message)}" for s in data.audience_segments[:4]]
    lines += ["", "<b>Цели</b>"] + [f"• {q(g)}" for g in data.goals]
    lines += ["", f"<b>Тон.</b> {q(data.tone_of_voice)}", "", "<b>Рубрики</b>"]
    lines += [
        f"• {q(r.name)} — {r.share_percent}% ({_goal(r.goal)}): {q(r.description)}" for r in data.rubrics
    ]
    lines += ["", "<b>Площадки</b>"]
    lines += [
        f"• {_PLATFORM_TITLES.get(p.platform, p.platform)}: {p.posts_per_week} в неделю, "
        f"{', '.join(p.best_times)}"
        for p in data.platforms
    ]
    if data.content_rules:
        lines += ["", "<b>Правила контента</b>"] + [f"• {q(r)}" for r in data.content_rules]
    if data.kpis:
        lines += ["", "<b>Что отслеживаем</b>"] + [f"• {q(k)}" for k in data.kpis]
    lines += ["", "Хотите другой акцент — напишите, например: «сделай упор на студентов»."]

    buttons = [[Button(text="🔄 Другой вариант", action=Action.STRATEGY_REDO)]]
    if status == DocumentStatus.DRAFT:
        buttons.insert(0, [Button(text="✅ Утвердить", action=Action.STRATEGY_APPROVE, value=strategy_id)])
    buttons.append([Button(text="📅 Составить контент-план", action=Action.PLAN_CREATE)])
    return Reply(text=_fit(lines), buttons=buttons, intent="show_strategy")


def _local(value: datetime | None, tz: str) -> datetime | None:
    return value.astimezone(ZoneInfo(tz)) if value else None


def when_label(value: datetime | None, tz: str) -> str:
    local = _local(value, tz)
    return f"{WEEKDAYS[local.weekday()]} {local:%d.%m}, {local:%H:%M}" if local else "без даты"


def plan_reply(
    project_name: str,
    plan: ContentPlan,
    items: list[Publication],
    *,
    timezone: str,
    written: set[object] | None = None,
    notes: list[str] | None = None,
) -> Reply:
    written = written or set()
    lines = [
        f"<b>Контент-план «{q(project_name)}»</b>",
        f"{plan.period_start:%d.%m} – {plan.period_end:%d.%m} · {len(items)} публикаций в Telegram · "
        f"{PLAN_STATUS.get(DocumentStatus(plan.status), plan.status)}",
    ]
    current_day = None
    for item in items:
        local = _local(item.planned_at, timezone)
        day = local.date() if local else None
        if day != current_day:
            current_day = day
            lines += ["", f"<b>{WEEKDAYS[local.weekday()]} {local:%d.%m}</b>" if local else "<b>Без даты</b>"]
        mark = " ✍️" if item.id in written else ""
        time_label = f"{local:%H:%M}" if local else "--:--"
        lines.append(f"{time_label} · {q(item.category)} · «{q(item.title)}»{mark}")
    if notes:
        lines += ["", *(f"ℹ️ {q(n)}" for n in notes)]
    if written:
        lines += ["", "✍️ — текст уже написан"]

    plan_id = str(plan.id)
    buttons: list[list[Button]] = []
    if plan.status == DocumentStatus.DRAFT:
        buttons.append([Button(text="✅ Утвердить план", action=Action.PLAN_APPROVE, value=plan_id)])
    buttons.append([Button(text="🔄 Пересоставить", action=Action.PLAN_REDO, value=plan_id)])
    if len(written) < len(items):
        buttons.append([Button(text="✍️ Написать тексты", action=Action.PLAN_WRITE, value=plan_id)])
    return Reply(text=_fit(lines), buttons=buttons, intent="show_plan")


def post_preview(
    publication: Publication,
    text: str,
    *,
    warnings: list[str],
    timezone: str,
    index: int | None = None,
    total: int | None = None,
) -> Reply:
    head = f"<b>Пост {index}/{total}</b>" if index and total else "<b>Черновик поста</b>"
    meta = [when_label(publication.planned_at, timezone)] if publication.planned_at else []
    meta += [q(publication.category), f"цель: {_goal(publication.goal)}"]
    lines = [f"{head} · " + " · ".join(meta), "", text]
    if publication.visual_idea:
        lines += ["", f"🖼 <i>Идея картинки: {q(publication.visual_idea)}</i>"]
    if warnings:
        lines += ["", "⚠️ <b>Проверьте перед публикацией:</b>", *(f"• {q(w)}" for w in warnings)]
    return Reply(text=_fit(lines), intent="draft_post")
