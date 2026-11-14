"""Распознавание намерения пользователя по свободному тексту."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.ai.llm.base import LLMMessage, ModelProfile
from app.ai.llm.service import CallContext, LLMService
from app.ai.prompts import load_prompt


class Intent(StrEnum):
    DESCRIBE_BUSINESS = "describe_business"
    ADD_WEBSITE = "add_website"
    SHOW_PROFILE = "show_profile"
    CREATE_PROJECT = "create_project"
    SWITCH_PROJECT = "switch_project"
    LIST_PROJECTS = "list_projects"
    CREATE_STRATEGY = "create_strategy"
    CREATE_PLAN = "create_plan"
    CREATE_POST = "create_post"
    EDIT_POST = "edit_post"
    GENERATE_IMAGE = "generate_image"
    SCHEDULE_POST = "schedule_post"
    SHOW_SCHEDULE = "show_schedule"
    CANCEL_POST = "cancel_post"
    ANALYTICS = "analytics"
    FIND_TOPICS = "find_topics"
    SETTINGS = "settings"
    PAUSE = "pause"
    RESUME = "resume"
    HELP = "help"
    SMALLTALK = "smalltalk"
    UNKNOWN = "unknown"


_OPTIONAL_TEXT_FIELDS = (
    "project_name",
    "business_name",
    "topic",
    "instructions",
    "when",
    "clarification_question",
)


class IntentResult(BaseModel):
    """Ответ классификатора. Мягкая нормализация: модели часто путают пустые значения и границы."""

    model_config = ConfigDict(extra="ignore")

    intent: Intent = Field(description="Намерение пользователя")
    confidence: float = Field(description="Уверенность от 0 до 1")
    period_days: int | None = Field(default=None, description="Период в днях для плана или отчёта")
    project_name: str | None = Field(default=None, description="Название проекта")
    business_name: str | None = Field(default=None, description="Название бизнеса")
    topic: str | None = Field(default=None, description="Тема поста или картинки")
    instructions: str | None = Field(default=None, description="Пожелания или что изменить в тексте")
    when: str | None = Field(default=None, description="Когда опубликовать, словами пользователя")
    clarification_question: str | None = Field(default=None, description="Уточняющий вопрос пользователю")

    @field_validator("intent", mode="before")
    @classmethod
    def _unknown_intent(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip().lower() in {i.value for i in Intent}:
            return value.strip().lower()
        return Intent.UNKNOWN

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, value: Any) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        return min(max(number, 0.0), 1.0)

    @field_validator("period_days", mode="before")
    @classmethod
    def _clamp_period(cls, value: Any) -> int | None:
        if value in (None, "", 0, "0"):
            return None
        try:
            days = int(float(value))
        except (TypeError, ValueError):
            return None
        return min(max(days, 1), 90)

    @field_validator(*_OPTIONAL_TEXT_FIELDS, mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        if value is None:
            return None
        text = " ".join(str(value).split())
        if not text or text.lower() in {"null", "none", "нет"}:
            return None
        return text[:300]


CONFIDENCE_THRESHOLD = 0.55


class IntentClassifier:
    """Классификатор на модели SMART: по замеру 14.11.2026 (scripts/eval_intents.py)
    GigaChat-3-Ultra — 31/31 за ~1,6 с, GigaChat-2 (Lite) — 29/31 за ~0,7 с.
    """

    def __init__(self, llm: LLMService, *, profile: ModelProfile = ModelProfile.SMART) -> None:
        self._llm = llm
        self._profile = profile

    async def classify(
        self, text: str, *, history: list[LLMMessage] | None = None, ctx: CallContext
    ) -> IntentResult:
        messages = [
            LLMMessage(role="system", content=load_prompt("intent_classifier")),
            *(history or []),
            LLMMessage(role="user", content=text),
        ]
        return await self._llm.structured(
            messages, IntentResult, ctx=ctx, profile=self._profile, max_tokens=300, temperature=0.0
        )
