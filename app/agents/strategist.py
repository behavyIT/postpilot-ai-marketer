"""Marketing Strategist: стратегия продвижения по профилю бизнеса."""

from __future__ import annotations

import re

from app.ai.llm.base import LLMMessage, ModelProfile
from app.ai.llm.service import CallContext, LLMService
from app.ai.prompts import load_prompt
from app.domain.enums import ContentGoal, Platform
from app.schemas.business_profile import FIELD_LABELS, BusinessProfileData
from app.schemas.content import PlatformPlan, Rubric, StrategyData

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
DEFAULT_TIMES = ["09:00", "13:00", "19:00"]
MIN_RUBRICS, MAX_RUBRICS = 3, 6


def profile_as_text(data: BusinessProfileData) -> str:
    lines = [f"Кратко: {data.summary}"] if data.summary else []
    for name, label in FIELD_LABELS.items():
        value = data.get(name)
        if value.value:
            mark = "со слов владельца" if value.status == "confirmed" else "из сайта и документов"
            lines.append(f"- {label} ({mark}): {value.value}")
    return "\n".join(lines)


def preferences_as_text(preferences: list[str]) -> str:
    if not preferences:
        return "Пожеланий пока нет."
    return "\n".join(f"- {p}" for p in preferences)


class Strategist:
    def __init__(self, llm: LLMService) -> None:
        self._llm = llm

    async def create(
        self,
        profile: BusinessProfileData,
        *,
        platforms: list[Platform],
        preferences: list[str],
        previous: StrategyData | None,
        instructions: str | None,
        ctx: CallContext,
    ) -> StrategyData:
        parts = [
            f"Профиль бизнеса:\n{profile_as_text(profile)}",
            "Доступные площадки: " + ", ".join(p.value for p in platforms),
            f"Пожелания владельца:\n{preferences_as_text(preferences)}",
        ]
        if previous is not None:
            parts.append(f"Предыдущий вариант стратегии (владелец просит другой): {previous.summary}")
        if instructions:
            parts.append(f"Что владелец просит учесть сейчас: {instructions}")
        draft = await self._llm.structured(
            [
                LLMMessage(role="system", content=load_prompt("strategist")),
                LLMMessage(role="user", content="\n\n".join(parts)),
            ],
            StrategyData,
            ctx=ctx,
            profile=ModelProfile.SMART,
            max_tokens=4000,
            temperature=0.4,
        )
        return normalize_strategy(draft, platforms)


def normalize_strategy(data: StrategyData, platforms: list[Platform]) -> StrategyData:
    """Приводит ответ модели к допустимым значениям: доли = 100%, разумная частота, корректное время."""
    rubrics = [r for r in data.rubrics if r.name.strip()][:MAX_RUBRICS]
    for rubric in rubrics:
        rubric.name = rubric.name.strip()[:80]
        rubric.goal = (
            rubric.goal if rubric.goal in {g.value for g in ContentGoal} else ContentGoal.ENGAGEMENT.value
        )
        rubric.share_percent = max(1, min(rubric.share_percent, 100))
    if len(rubrics) < MIN_RUBRICS:
        rubrics.extend(_default_rubrics()[: MIN_RUBRICS - len(rubrics)])
    _normalize_shares(rubrics)

    allowed = {p.value for p in platforms}
    plans: dict[str, PlatformPlan] = {}
    for plan in data.platforms:
        if plan.platform not in allowed or plan.platform in plans:
            continue
        plan.posts_per_week = max(1, min(plan.posts_per_week, 14))
        times = [t for t in (_fix_time(x) for x in plan.best_times) if t]
        plan.best_times = sorted(dict.fromkeys(times))[:4] or DEFAULT_TIMES
        plans[plan.platform] = plan
    for platform in platforms:
        if platform.value not in plans:
            plans[platform.value] = PlatformPlan(
                platform=platform.value, posts_per_week=3, best_times=DEFAULT_TIMES, notes=""
            )
    data.rubrics = rubrics
    data.platforms = list(plans.values())
    return data


def _normalize_shares(rubrics: list[Rubric]) -> None:
    total = sum(r.share_percent for r in rubrics)
    for rubric in rubrics:
        rubric.share_percent = round(rubric.share_percent * 100 / total)
    diff = 100 - sum(r.share_percent for r in rubrics)
    if diff:
        max(rubrics, key=lambda r: r.share_percent).share_percent += diff


def _fix_time(value: str) -> str | None:
    match = _TIME_RE.match(value.strip())
    return f"{int(match.group(1)):02d}:{match.group(2)}" if match else None


def _default_rubrics() -> list[Rubric]:
    return [
        Rubric(
            name="Полезное",
            goal="education",
            share_percent=40,
            description="Советы и знания",
            example_topics=[],
        ),
        Rubric(
            name="О нас", goal="loyalty", share_percent=30, description="Люди и закулисье", example_topics=[]
        ),
        Rubric(
            name="Предложения",
            goal="sales",
            share_percent=30,
            description="Продукты и услуги",
            example_topics=[],
        ),
    ]
