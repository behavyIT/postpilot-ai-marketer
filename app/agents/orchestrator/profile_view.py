"""Представление профиля бизнеса и отчёта об изучении источников в чате."""

from __future__ import annotations

from urllib.parse import urlsplit

from app.agents.orchestrator.messages import q
from app.agents.orchestrator.replies import Action, Button, Reply
from app.domain.enums import FactStatus
from app.schemas.business_profile import FIELD_LABELS, BusinessProfileData

MARKS = {FactStatus.CONFIRMED: "✅", FactStatus.INFERRED: "🔎"}
LEGEND = "✅ — с ваших слов, 🔎 — из сайта и документов"
ANSWER_HINT = "Отвечайте обычным сообщением — я дополню профиль."
REFRESH_BUTTON = [Button(text="🔄 Обновить профиль", action=Action.PROFILE_REFRESH)]
_BUDGETS = (320, 200, 120)  # если текст не влезает в лимит Telegram, сокращаем значения сильнее


def format_profile(
    project_name: str,
    data: BusinessProfileData,
    *,
    version: int | None,
    questions: list[str],
    limit: int = 3900,
) -> str:
    text = ""
    for budget in _BUDGETS:
        text = _render(project_name, data, version=version, questions=questions, budget=budget)
        if len(text) <= limit:
            break
    return text


def _render(
    project_name: str, data: BusinessProfileData, *, version: int | None, questions: list[str], budget: int
) -> str:
    head = f"<b>Профиль бизнеса «{q(project_name)}»</b>\nзаполнено {data.filled} из 14"
    if version:
        head += f" · версия {version}"
    lines = [head]
    if data.summary:
        lines.append(f"\n{q(data.summary)}")
    lines.append("")
    for name, label in FIELD_LABELS.items():
        value = data.get(name)
        if value.value:
            lines.append(f"{MARKS.get(value.status, '•')} <b>{label}:</b> {q(_cut(value.value, budget))}")
    if data.missing:
        lines.append("\n❓ Нет данных: " + ", ".join(FIELD_LABELS[f].lower() for f in data.missing))
    lines.append(f"\n<i>{LEGEND}</i>")
    if questions:
        lines.append("\n<b>Уточните, пожалуйста:</b>")
        lines.extend(f"{i}. {q(question)}" for i, question in enumerate(questions[:5], start=1))
        lines.append(ANSWER_HINT)
    return "\n".join(lines)


def _cut(value: str, budget: int) -> str:
    return value if len(value) <= budget else value[: budget - 1].rstrip() + "…"


def profile_reply(
    project_name: str, data: BusinessProfileData, *, version: int | None, questions: list[str]
) -> Reply:
    return Reply(
        text=format_profile(project_name, data, version=version, questions=questions),
        buttons=[REFRESH_BUTTON],
        intent="show_profile",
    )


def analysis_summary(
    *,
    websites: list[tuple[str, int]],
    documents: list[str],
    facts_added: int,
    facts_rejected: int,
    failures: list[str],
    suspicious: list[str],
) -> str:
    parts: list[str] = []
    studied = [f"сайт {q(_host(url))} ({pages} стр.)" for url, pages in websites]
    studied += [f"«{q(title)}»" for title in documents]
    if studied:
        parts.append("Изучил: " + ", ".join(studied) + ".")
    if facts_added:
        line = f"Нашёл новых фактов о бизнесе: {facts_added}."
        if facts_rejected:
            line += f" Ещё {facts_rejected} отбросил — их не удалось подтвердить цитатой из источника."
        parts.append(line)
    elif not failures:
        parts.append("Новых сведений не нашёл.")
    parts.extend(f"⚠️ {q(failure)}" for failure in failures[:5])
    if suspicious:
        parts.append(
            "⚠️ В источниках есть текст, похожий на команды для ИИ. Я его не выполнял, "
            "но проверьте: " + ", ".join(q(s) for s in suspicious[:3])
        )
    return "\n".join(parts)


def _host(url: str) -> str:
    return urlsplit(url).hostname or url
