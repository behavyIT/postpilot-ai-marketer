"""Copywriter: текст поста под площадку по пункту контент-плана.

После генерации код проверяет технические ограничения площадки и утверждения в тексте
(цифры вне профиля, превосходные степени, гарантии). Нарушения не скрываются: они
сохраняются как предупреждения и показываются владельцу перед согласованием.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.agents.compliance import check_claims
from app.agents.content_planner import normalize_hashtags
from app.agents.strategist import preferences_as_text
from app.ai.llm.base import LLMMessage, ModelProfile
from app.ai.llm.service import CallContext, LLMService
from app.ai.prompts import load_prompt
from app.domain.platforms import PlatformSpec, strip_tags, validate_for_platform, visible_length
from app.schemas.content import PostDraft, StrategyData

TARGET_LENGTH = 900  # укладываемся в подпись к картинке Telegram (1024) с запасом


@dataclass(frozen=True, slots=True)
class PostBrief:
    category: str
    goal: str
    topic: str
    title: str
    brief: str
    cta: str
    hashtags: list[str]
    visual_idea: str


@dataclass(frozen=True, slots=True)
class NumberedFact:
    number: int
    text: str


@dataclass(slots=True)
class WrittenPost:
    text: str  # итоговый текст с хештегами — ровно то, что будет опубликовано
    hashtags: list[str]
    used_fact_numbers: list[int]
    warnings: list[str] = field(default_factory=list)


class Copywriter:
    def __init__(self, llm: LLMService) -> None:
        self._llm = llm

    async def write(
        self,
        brief: PostBrief,
        *,
        spec: PlatformSpec,
        strategy: StrategyData | None,
        facts: list[NumberedFact],
        preferences: list[str],
        instructions: str | None,
        ctx: CallContext,
        reference_text: str | None = None,
    ) -> WrittenPost:
        """reference_text — все проверенные сведения о бизнесе для проверки цифр (шире, чем facts)."""
        task = (
            f"Рубрика: {brief.category}. Цель: {brief.goal}.\nТема: {brief.topic}\nЗаголовок: {brief.title}\n"
            f"О чём пост: {brief.brief}\nПризыв к действию: {brief.cta}\n"
            f"Хештеги-подсказки: {' '.join(brief.hashtags) or 'нет'}"
        )
        parts = [
            f"Площадка: {spec.title}. {spec.style_hint} Длина — до {TARGET_LENGTH} символов.",
            f"Задание:\n{task}",
            "Факты о бизнесе:\n" + ("\n".join(f"[{f.number}] {f.text}" for f in facts) or "нет"),
            f"Пожелания владельца:\n{preferences_as_text(preferences)}",
        ]
        if strategy is not None:
            parts.append(
                f"Тон бренда: {strategy.tone_of_voice}\nПравила контента:\n"
                + "\n".join(f"- {r}" for r in strategy.content_rules)
            )
        if instructions:
            parts.append(f"Что владелец просит учесть: {instructions}")
        messages = [
            LLMMessage(role="system", content=load_prompt("copywriter")),
            LLMMessage(role="user", content="\n\n".join(parts)),
        ]
        draft = await self._llm.structured(
            messages, PostDraft, ctx=ctx, profile=ModelProfile.SMART, max_tokens=2000, temperature=0.7
        )

        body = _clean_body(draft.body, spec)
        if visible_length(body) > TARGET_LENGTH + 100:
            # Одна попытка сократить; если не вышло — оставляем и предупреждаем владельца.
            shortened = await self._llm.structured(
                [
                    *messages,
                    LLMMessage(role="assistant", content=draft.body[:4000]),
                    LLMMessage(
                        role="user",
                        content=f"Сократи пост до {TARGET_LENGTH} символов, сохрани смысл, факты и призыв.",
                    ),
                ],
                PostDraft,
                ctx=ctx,
                profile=ModelProfile.SMART,
                max_tokens=2000,
            )
            body, draft = _clean_body(shortened.body, spec), shortened

        hashtags = normalize_hashtags(draft.hashtags or brief.hashtags, limit=spec.max_hashtags)
        text = body + (f"\n\n{' '.join(hashtags)}" if hashtags else "")
        known = {f.number for f in facts}
        warnings = validate_for_platform(text, spec, with_image=True)
        warnings += check_claims(text, reference_text or "\n".join(f.text for f in facts))
        return WrittenPost(
            text=text,
            hashtags=hashtags,
            used_fact_numbers=[n for n in dict.fromkeys(draft.used_fact_ids) if n in known],
            warnings=warnings,
        )


_HASHTAG_LINE = re.compile(r"(?m)^\s*(#[\w]+\s*)+$")
# Номера фактов вида [3] или [15, 32]: модель иногда вставляет их в текст (замер 14.11.2026).
_FACT_REFS = re.compile(r"\s?\[\d+(?:\s*,\s*\d+)*\]")


def _clean_body(body: str, spec: PlatformSpec) -> str:
    """Убирает хештеги, которые модель всё же вписала в текст, и лишние пустые строки.

    Если разметка недопустима для площадки, текст переводится в обычный.
    """
    text = _FACT_REFS.sub("", _HASHTAG_LINE.sub("", body.replace("\r\n", "\n"))).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if validate_for_platform(text, spec, with_image=False) and any(
        "разметка" in p or "теги" in p for p in validate_for_platform(text, spec, with_image=False)
    ):
        text = strip_tags(text)
    return text
