"""Visual: картинка к посту.

Сюжет берётся из идеи визуала в контент-плане (или из просьбы владельца), а рисует
модель с функцией генерации изображений. Текст на картинке запрещён промптом:
генераторы плохо пишут по-русски, а надпись с ошибкой хуже, чем её отсутствие.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.ai.llm.base import GeneratedImage, LLMMessage
from app.ai.llm.service import CallContext, LLMService
from app.ai.prompts import load_prompt

MAX_SUBJECT = 600


@dataclass(frozen=True, slots=True)
class ImageBrief:
    topic: str
    visual_idea: str | None
    industry: str | None
    request: str | None = None  # пожелание владельца: «с котом на подоконнике»


def build_image_prompt(brief: ImageBrief) -> str:
    subject = (brief.request or brief.visual_idea or brief.topic).strip()[:MAX_SUBJECT]
    parts = [f"Нарисуй картинку к посту. Сюжет: {subject}."]
    if brief.request and brief.visual_idea:
        parts.append(f"Исходная идея из контент-плана: {brief.visual_idea.strip()[:300]}.")
    if brief.industry:
        parts.append(f"Сфера бизнеса: {brief.industry.strip()[:200]}.")
    parts.append("Без текста и надписей на изображении, без логотипов.")
    return " ".join(parts)


class VisualAgent:
    def __init__(self, llm: LLMService) -> None:
        self._llm = llm

    @property
    def available(self) -> bool:
        return self._llm.supports_image_generation

    async def draw(self, prompt: str, *, ctx: CallContext) -> GeneratedImage:
        return await self._llm.generate_image(
            [
                LLMMessage(role="system", content=load_prompt("visual")),
                LLMMessage(role="user", content=prompt),
            ],
            ctx=ctx,
        )
