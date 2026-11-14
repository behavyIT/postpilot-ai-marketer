"""Контракт провайдера языковой модели.

Приложение не зависит от конкретного API: роли агентов работают через LLMService,
а он — через LLMProvider. Новый провайдер (Ollama, OpenAI-совместимый, Claude)
добавляется реализацией этого протокола.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol


class ModelProfile(StrEnum):
    """Профиль задачи; конкретная модель задаётся в настройках."""

    SMART = "smart"  # стратегия, контент-план, тексты
    FAST = "fast"  # классификация, извлечение, проверки
    VISION = "vision"  # распознавание изображений


Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True, slots=True)
class LLMMessage:
    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class ImageInput:
    data: bytes
    mime_type: str
    filename: str = "image"


@dataclass(frozen=True, slots=True)
class LLMRequest:
    model: str
    messages: list[LLMMessage]
    max_tokens: int = 1024
    temperature: float | None = None
    # JSON Schema ответа: провайдер просит модель вернуть JSON строго по ней.
    json_schema: dict[str, Any] | None = None
    # Изображения прикладываются к последнему сообщению пользователя.
    images: tuple[ImageInput, ...] = ()


@dataclass(frozen=True, slots=True)
class LLMUsageInfo:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str
    model: str
    finish_reason: str | None = None
    usage: LLMUsageInfo = field(default_factory=LLMUsageInfo)


@dataclass(frozen=True, slots=True)
class GeneratedImage:
    data: bytes
    mime_type: str
    model: str
    usage: LLMUsageInfo = field(default_factory=LLMUsageInfo)


class LLMProvider(Protocol):
    name: str
    # Умеет ли API принимать JSON Schema ответа параметром. Если да, схему не дублируем
    # текстом в промпте: у GigaChat Lite дублирование даёт пустые ответы (замер 14.11.2026).
    supports_json_schema: bool
    supports_images: bool
    supports_image_generation: bool

    async def complete(self, request: LLMRequest) -> LLMResponse: ...

    async def generate_image(self, model: str, messages: list[LLMMessage]) -> GeneratedImage:
        """Нарисовать картинку по описанию. Вызывается, только если supports_image_generation."""
        ...

    async def aclose(self) -> None: ...


class LLMError(Exception):
    """Базовая ошибка обращения к модели. Текст подходит для показа пользователю."""


class LLMUnavailableError(LLMError):
    """Сеть, таймаут или ошибка сервера провайдера — можно повторить позже."""


class LLMRateLimitError(LLMUnavailableError):
    """Провайдер ограничил частоту запросов."""


class LLMBusyError(LLMUnavailableError):
    """Не дождались своей очереди: бесплатный тариф обрабатывает один запрос за раз."""


class LLMConfigError(LLMError):
    """Неверный ключ, модель или параметры запроса — повтор не поможет."""


class LLMOutputError(LLMError):
    """Модель вернула ответ, который не прошёл проверку даже после попытки исправления."""
