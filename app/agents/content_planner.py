"""Content Planner: контент-план на период по стратегии.

Модель предлагает пункты, а код проверяет и чинит то, что легко проверить:
даты в периоде, корректное время, рубрики из стратегии, количество публикаций,
интервалы между постами и повторы тем (между собой и с недавними публикациями).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.agents.business_analyst import normalize_for_match
from app.agents.strategist import preferences_as_text, profile_as_text
from app.ai.llm.base import LLMMessage, ModelProfile
from app.ai.llm.service import CallContext, LLMService
from app.ai.prompts import load_prompt
from app.domain.calendar import events_between
from app.domain.enums import ContentGoal, Platform
from app.schemas.business_profile import BusinessProfileData
from app.schemas.content import ContentPlanDraft, PlanItem, StrategyData

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
SIMILARITY_THRESHOLD = 0.6
MIN_GAP = timedelta(hours=3)
MAX_POSTS_PER_DAY = 2
_WORD = re.compile(r"[a-zа-я0-9]+")


@dataclass(slots=True)
class PlannedItem:
    planned_at: datetime  # UTC
    platform: Platform
    category: str
    goal: ContentGoal
    topic: str
    title: str
    brief: str
    cta: str
    hashtags: list[str]
    visual_idea: str


@dataclass(slots=True)
class PlanResult:
    items: list[PlannedItem]
    notes: list[str] = field(default_factory=list)


def target_counts(strategy: StrategyData, days: int) -> dict[str, int]:
    return {p.platform: max(1, math.ceil(p.posts_per_week * days / 7)) for p in strategy.platforms}


class ContentPlanner:
    def __init__(self, llm: LLMService) -> None:
        self._llm = llm

    async def create(
        self,
        *,
        profile: BusinessProfileData,
        strategy: StrategyData,
        start: date,
        days: int,
        timezone: str,
        recent_topics: list[str],
        preferences: list[str],
        instructions: str | None,
        ctx: CallContext,
    ) -> PlanResult:
        end = start + timedelta(days=days - 1)
        counts = target_counts(strategy, days)
        dates = ", ".join(
            f"{(start + timedelta(days=i)).isoformat()} ({WEEKDAYS[(start + timedelta(days=i)).weekday()]})"
            for i in range(days)
        )
        events = events_between(start, end)
        parts = [
            f"Период: с {start.isoformat()} по {end.isoformat()}. Дни: {dates}",
            "Сколько публикаций: " + ", ".join(f"{platform} — {n}" for platform, n in counts.items()),
            "Праздники и события в периоде: "
            + ("; ".join(f"{e.day.isoformat()} — {e.title}" for e in events) or "нет"),
            _strategy_text(strategy),
            f"Профиль бизнеса:\n{profile_as_text(profile)}",
            "Недавние публикации (не повторять темы):\n"
            + ("\n".join(f"- {t}" for t in recent_topics[:30]) or "нет"),
            f"Пожелания владельца:\n{preferences_as_text(preferences)}",
        ]
        if instructions:
            parts.append(f"Что владелец просит учесть сейчас: {instructions}")
        draft = await self._llm.structured(
            [
                LLMMessage(role="system", content=load_prompt("content_planner")),
                LLMMessage(role="user", content="\n\n".join(parts)),
            ],
            ContentPlanDraft,
            ctx=ctx,
            profile=ModelProfile.SMART,
            max_tokens=6000,
            temperature=0.6,
        )
        return validate_plan(
            draft.items,
            strategy=strategy,
            start=start,
            end=end,
            timezone=timezone,
            counts=counts,
            recent_topics=recent_topics,
        )


def _strategy_text(strategy: StrategyData) -> str:
    rubrics = "\n".join(
        f"- {r.name} ({r.goal}, {r.share_percent}%): {r.description}" for r in strategy.rubrics
    )
    platforms = "\n".join(
        f"- {p.platform}: {p.posts_per_week} в неделю, время {', '.join(p.best_times)}. {p.notes}"
        for p in strategy.platforms
    )
    rules = "\n".join(f"- {rule}" for rule in strategy.content_rules)
    return (
        f"Стратегия: {strategy.summary}\nТон: {strategy.tone_of_voice}\n"
        f"Рубрики:\n{rubrics}\nПлощадки:\n{platforms}\nПравила контента:\n{rules}"
    )


def _words(text: str) -> set[str]:
    return {w[:5] for w in _WORD.findall(normalize_for_match(text)) if len(w) > 3}


def similar(a: str, b: str) -> bool:
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) >= SIMILARITY_THRESHOLD


def _parse_time(value: str, fallback: str) -> time:
    for candidate in (value.strip(), fallback):
        match = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", candidate)
        if match:
            return time(int(match.group(1)), int(match.group(2)))
    return time(12, 0)


def validate_plan(
    items: list[PlanItem],
    *,
    strategy: StrategyData,
    start: date,
    end: date,
    timezone: str,
    counts: dict[str, int],
    recent_topics: list[str],
) -> PlanResult:
    tz = ZoneInfo(timezone)
    rubrics = {r.name.casefold(): r for r in strategy.rubrics}
    times = {p.platform: p.best_times for p in strategy.platforms}
    notes: list[str] = []
    accepted: list[PlannedItem] = []
    per_platform: dict[str, int] = {}
    dropped_dates = dropped_repeats = 0

    for item in items:
        platform = item.platform if item.platform in counts else next(iter(counts))
        if per_platform.get(platform, 0) >= counts[platform]:
            continue
        try:
            day = date.fromisoformat(item.date.strip())
        except ValueError:
            dropped_dates += 1
            continue
        if not start <= day <= end:
            dropped_dates += 1
            continue
        topic = f"{item.topic} {item.title}"
        if any(similar(topic, t) for t in recent_topics) or any(
            similar(topic, f"{a.topic} {a.title}") for a in accepted
        ):
            dropped_repeats += 1
            continue

        rubric = rubrics.get(item.rubric.strip().casefold()) or next(
            (
                r
                for key, r in rubrics.items()
                if key in item.rubric.casefold() or item.rubric.casefold() in key
            ),
            None,
        )
        goal_value = item.goal if item.goal in {g.value for g in ContentGoal} else None
        goal = ContentGoal(goal_value or (rubric.goal if rubric else ContentGoal.ENGAGEMENT.value))
        local = datetime.combine(day, _parse_time(item.time, times.get(platform, ["12:00"])[0]), tzinfo=tz)
        planned_at = _fit_slot(local, [a.planned_at for a in accepted if a.platform == platform], tz)
        if planned_at is None:
            continue
        accepted.append(
            PlannedItem(
                planned_at=planned_at,
                platform=Platform(platform),
                category=(rubric.name if rubric else item.rubric.strip() or "Другое")[:80],
                goal=goal,
                topic=item.topic.strip()[:300],
                title=(item.title.strip() or item.topic.strip())[:300],
                brief=item.brief.strip()[:1000],
                cta=item.cta.strip()[:300],
                hashtags=normalize_hashtags(item.hashtags, limit=4),
                visual_idea=item.visual_idea.strip()[:1000],
            )
        )
        per_platform[platform] = per_platform.get(platform, 0) + 1

    if dropped_dates:
        notes.append(f"Убрал пунктов с датой вне периода: {dropped_dates}")
    if dropped_repeats:
        notes.append(f"Убрал повторяющихся тем: {dropped_repeats}")
    for platform, needed in counts.items():
        got = per_platform.get(platform, 0)
        if got < needed:
            notes.append(f"{platform}: получилось {got} публикаций из {needed} запланированных")
    accepted.sort(key=lambda a: a.planned_at)
    return PlanResult(items=accepted, notes=notes)


def _fit_slot(local: datetime, taken: list[datetime], tz: ZoneInfo) -> datetime | None:
    """Не больше двух постов в день и не чаще раза в 3 часа на одной площадке."""
    candidate = local.astimezone(ZoneInfo("UTC"))
    same_day = [t for t in taken if t.astimezone(tz).date() == local.date()]
    if len(same_day) >= MAX_POSTS_PER_DAY:
        return None
    while any(abs(candidate - t) < MIN_GAP for t in same_day):
        candidate += MIN_GAP
        if candidate.astimezone(tz).date() != local.date():
            return None
    return candidate


def normalize_hashtags(values: list[str], *, limit: int) -> list[str]:
    tags: list[str] = []
    for value in values:
        tag = re.sub(r"[^\w]", "", value.strip().lstrip("#"))
        if tag and f"#{tag}".lower() not in {t.lower() for t in tags}:
            tags.append(f"#{tag}")
    return tags[:limit]
