"""Business Analyst: факты о бизнесе из источников и профиль на их основе.

Два шага с разными промптами:
1. extract_facts — из фрагментов источников (недоверенные данные) извлекаются факты,
   каждый с дословной цитатой. Факт, цитаты которого нет в тексте, отбрасывается кодом:
   так модель не может «придумать» сведения о компании.
2. synthesize — из проверенных фактов собирается профиль; каждое значение ссылается
   на номера фактов, значения без опоры на факты отбрасываются.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, JsonValue, field_validator

from app.ai.llm.base import LLMMessage, ModelProfile
from app.ai.llm.service import CallContext, LLMService
from app.ai.prompts import load_prompt
from app.domain.enums import FactStatus, ProfileField, SourceKind

MAX_CHUNK_CHARS = 9000
MAX_FACTS_PER_FIELD = 8
_FIELD_ENUM: list[JsonValue] = [f.value for f in ProfileField]
_INJECTION_RE = re.compile(
    r"(ignore (all |the )?(previous|above) instructions|system prompt|"
    r"игнорируй (все |предыдущие |свои )?инструкции|забудь (все )?инструкции|ты больше не)",
    re.IGNORECASE,
)


# --- Схемы ответов модели -------------------------------------------------------------------


class ExtractedFact(BaseModel):
    field: str = Field(json_schema_extra={"enum": _FIELD_ENUM}, description="Пункт профиля")
    value: str = Field(description="Факт кратко, до 300 символов")
    quote: str = Field(description="Дословная цитата из источника, до 300 символов")


class FactExtraction(BaseModel):
    # Поля обязательные: без required в схеме GigaChat возвращает плоский словарь «пункт → значение»
    # без цитат (замер 14.11.2026), а значения по умолчанию молча превращали это в пустой список.
    facts: list[ExtractedFact]
    suspicious_instructions: bool = Field(description="В тексте есть указания для ИИ")


class SynthesizedField(BaseModel):
    field: str = Field(json_schema_extra={"enum": _FIELD_ENUM})
    value: str = Field(description="Итоговая формулировка, до 500 символов")
    fact_ids: list[int] = Field(description="Номера фактов-оснований")


class ProfileSynthesis(BaseModel):
    summary: str = Field(description="2–3 предложения о бизнесе")
    fields: list[SynthesizedField]
    questions: list[str] = Field(description="До 5 уточняющих вопросов владельцу")

    @field_validator("questions", mode="before")
    @classmethod
    def _limit_questions(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [str(q).strip()[:300] for q in value if str(q).strip()][:5]
        return value


# --- Данные для анализа ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceSegment:
    """Текст одного источника: заметки, страницы сайта или документа."""

    kind: SourceKind
    source_id: UUID
    label: str
    text: str


@dataclass(frozen=True, slots=True)
class VerifiedFact:
    field: ProfileField
    value: str
    quote: str
    kind: SourceKind
    source_id: UUID
    label: str

    @property
    def status(self) -> FactStatus:
        return FactStatus.CONFIRMED if self.kind is SourceKind.NOTE else FactStatus.INFERRED


@dataclass(frozen=True, slots=True)
class KnownFact:
    """Факт из базы для синтеза профиля."""

    number: int
    field: ProfileField
    value: str
    status: FactStatus
    source_label: str
    quote: str = ""


@dataclass(slots=True)
class ExtractionResult:
    facts: list[VerifiedFact]
    rejected: int
    suspicious: bool


def make_chunks(segments: list[SourceSegment], *, limit: int = MAX_CHUNK_CHARS) -> list[list[SourceSegment]]:
    """Группирует источники в порции для одного запроса; длинные тексты режет по абзацам."""
    pieces: list[SourceSegment] = []
    for segment in segments:
        text = segment.text.strip()
        if not text:
            continue
        while len(text) > limit:
            cut = text.rfind("\n", 0, limit)
            cut = cut if cut > limit // 2 else limit
            pieces.append(SourceSegment(segment.kind, segment.source_id, segment.label, text[:cut]))
            text = text[cut:].lstrip()
        if text:
            pieces.append(SourceSegment(segment.kind, segment.source_id, segment.label, text))

    chunks: list[list[SourceSegment]] = []
    current: list[SourceSegment] = []
    size = 0
    for piece in pieces:
        if current and size + len(piece.text) > limit:
            chunks.append(current)
            current, size = [], 0
        current.append(piece)
        size += len(piece.text)
    if current:
        chunks.append(current)
    return chunks


_QUOTE_CHARS = str.maketrans(
    {"«": '"', "»": '"', "“": '"', "”": '"', "„": '"', "’": "'", "ё": "е", "–": "-", "—": "-"}
)
_WS = re.compile(r"\s+")


def normalize_for_match(text: str) -> str:
    return _WS.sub(" ", text.lower().translate(_QUOTE_CHARS)).strip()


def find_source(quote: str, segments: list[SourceSegment]) -> SourceSegment | None:
    """Источник, в котором есть цитата. Части цитаты, разделённые «...», ищутся по отдельности."""
    parts = [normalize_for_match(p) for p in re.split(r"\.\.\.|…", quote)]
    parts = [p for p in parts if len(p) >= 3]
    if not parts:
        return None
    for segment in segments:
        haystack = normalize_for_match(segment.text)
        if all(part in haystack for part in parts):
            return segment
    return None


def looks_like_injection(text: str) -> bool:
    return bool(_INJECTION_RE.search(text))


# --- Агент ----------------------------------------------------------------------------------


class BusinessAnalyst:
    def __init__(self, llm: LLMService) -> None:
        self._llm = llm

    async def extract_facts(self, chunk: list[SourceSegment], *, ctx: CallContext) -> ExtractionResult:
        boundary = secrets.token_hex(4)  # непредсказуемая граница: текст источника не сможет «закрыть» тег
        blocks = "\n\n".join(
            f'<source id="{i}" boundary="{boundary}" kind="{s.kind.value}" title="{_attr(s.label)}">\n'
            f'{s.text}\n</source boundary="{boundary}">'
            for i, s in enumerate(chunk, start=1)
        )
        result = await self._llm.structured(
            [
                LLMMessage(role="system", content=load_prompt("fact_extraction")),
                LLMMessage(role="user", content=f"Источники:\n\n{blocks}"),
            ],
            FactExtraction,
            ctx=ctx,
            profile=ModelProfile.SMART,
            max_tokens=4000,
        )

        verified: list[VerifiedFact] = []
        rejected = 0
        seen: set[tuple[str, str]] = set()
        for fact in result.facts:
            try:
                field = ProfileField(fact.field.strip().lower())
            except ValueError:
                rejected += 1
                continue
            value, quote = fact.value.strip()[:400], fact.quote.strip()[:400]
            segment = find_source(quote, chunk) if value and quote else None
            if segment is None:
                rejected += 1  # цитаты нет в источнике — факт мог быть выдуман
                continue
            key = (field.value, normalize_for_match(value))
            if key in seen:
                continue
            seen.add(key)
            verified.append(VerifiedFact(field, value, quote, segment.kind, segment.source_id, segment.label))

        suspicious = result.suspicious_instructions or any(looks_like_injection(s.text) for s in chunk)
        return ExtractionResult(facts=verified, rejected=rejected, suspicious=suspicious)

    async def synthesize(self, facts: list[KnownFact], *, ctx: CallContext) -> ProfileSynthesis:
        lines = [f"[{f.number}] {f.field.value} | {_origin(f)} | {f.value}{_owner_quote(f)}" for f in facts]
        return await self._llm.structured(
            [
                LLMMessage(role="system", content=load_prompt("profile_synthesis")),
                LLMMessage(role="user", content="Факты:\n" + "\n".join(lines)),
            ],
            ProfileSynthesis,
            ctx=ctx,
            profile=ModelProfile.SMART,
            max_tokens=4000,
        )


def _owner_quote(fact: KnownFact) -> str:
    """Слова владельца даём дословно: при сжатии в value теряется смысл (например, чья это франшиза)."""
    if fact.status is FactStatus.CONFIRMED and fact.quote and fact.quote != fact.value:
        return f" (дословно: «{fact.quote[:300]}»)"
    return ""


def _origin(fact: KnownFact) -> str:
    return "сказал владелец" if fact.status is FactStatus.CONFIRMED else f"источник: {fact.source_label}"


def _attr(value: str) -> str:
    return value.replace('"', "'").replace("\n", " ")[:200]
