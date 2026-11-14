"""Структуры стратегии и контент-плана (хранятся в JSONB и отдаются модели как схема ответа).

Поля верхнего уровня без значений по умолчанию: иначе GigaChat может вернуть
«плоский» ответ, и он молча пройдёт проверку (см. профиль, замер 14.11.2026).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, JsonValue, field_validator

from app.domain.enums import ContentGoal, Platform

_GOALS: list[JsonValue] = [g.value for g in ContentGoal]
_PLATFORMS: list[JsonValue] = [p.value for p in Platform]

GOAL_LABELS = {
    ContentGoal.AWARENESS: "узнаваемость",
    ContentGoal.ENGAGEMENT: "вовлечение",
    ContentGoal.SALES: "продажи",
    ContentGoal.LOYALTY: "лояльность",
    ContentGoal.EDUCATION: "польза",
}


def _trim_list(value: Any, limit: int, length: int = 300) -> Any:
    if isinstance(value, list):
        return [str(v).strip()[:length] for v in value if str(v).strip()][:limit]
    return value


class Rubric(BaseModel):
    name: str = Field(description="Название рубрики, 1–4 слова")
    goal: str = Field(json_schema_extra={"enum": _GOALS}, description="Цель рубрики")
    share_percent: int = Field(description="Доля публикаций рубрики, %")
    description: str = Field(description="О чём рубрика, 1–2 предложения")
    example_topics: list[str] = Field(description="2–3 примера тем")

    @field_validator("example_topics", mode="before")
    @classmethod
    def _topics(cls, value: Any) -> Any:
        return _trim_list(value, 3, 200)


class AudienceSegment(BaseModel):
    name: str = Field(description="Сегмент аудитории")
    needs: str = Field(description="Что важно этим людям")
    message: str = Field(description="Что им говорить")


class PlatformPlan(BaseModel):
    platform: str = Field(json_schema_extra={"enum": _PLATFORMS})
    posts_per_week: int = Field(description="Публикаций в неделю")
    best_times: list[str] = Field(description="Удачное время публикаций, ЧЧ:ММ")
    notes: str = Field(description="Особенности подачи на площадке")


class StrategyData(BaseModel):
    summary: str = Field(description="Суть стратегии в 2–3 предложениях")
    positioning: str = Field(description="Позиционирование бренда")
    audience_segments: list[AudienceSegment] = Field(description="2–4 сегмента аудитории")
    goals: list[str] = Field(description="Цели продвижения на 1–3 месяца, до 5")
    tone_of_voice: str = Field(description="Тон и стиль общения")
    rubrics: list[Rubric] = Field(description="3–6 контентных рубрик")
    platforms: list[PlatformPlan] = Field(description="Площадки и частота публикаций")
    content_rules: list[str] = Field(description="Что делать и чего избегать в контенте, до 8 пунктов")
    kpis: list[str] = Field(description="Измеримые показатели, до 5")

    @field_validator("goals", "kpis", mode="before")
    @classmethod
    def _short_lists(cls, value: Any) -> Any:
        return _trim_list(value, 5)

    @field_validator("content_rules", mode="before")
    @classmethod
    def _rules(cls, value: Any) -> Any:
        return _trim_list(value, 8)


class PlanItem(BaseModel):
    date: str = Field(description="Дата ГГГГ-ММ-ДД")
    time: str = Field(description="Время ЧЧ:ММ")
    platform: str = Field(json_schema_extra={"enum": _PLATFORMS})
    rubric: str = Field(description="Название рубрики из стратегии")
    goal: str = Field(json_schema_extra={"enum": _GOALS})
    topic: str = Field(description="Тема")
    # Внутри пункта значения по умолчанию безопасны (форму задаёт список items), а GigaChat
    # регулярно пропускает title — без умолчания каждый план требовал повторного запроса.
    title: str = Field(default="", description="Заголовок поста")
    brief: str = Field(description="О чём пост, 1–3 предложения")
    cta: str = Field(default="", description="Призыв к действию")
    hashtags: list[str] = Field(default_factory=list, description="0–4 хештега")
    visual_idea: str = Field(default="", description="Идея картинки")


class ContentPlanDraft(BaseModel):
    items: list[PlanItem]


class PostDraft(BaseModel):
    body: str = Field(description="Готовый текст поста с HTML-разметкой площадки, без хештегов")
    hashtags: list[str] = Field(description="Хештеги, 0–5")
    used_fact_ids: list[int] = Field(description="Номера фактов из профиля, на которые опирается текст")
