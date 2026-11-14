"""LLMService — единственная точка обращения приложения к языковой модели.

Отвечает за: выбор модели по профилю, очередь (один поток на бесплатном тарифе),
учёт токенов, получение структурированного ответа с проверкой Pydantic
и одной попыткой исправления.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypeVar
from uuid import UUID

from pydantic import BaseModel, ValidationError

from app.ai.llm.base import (
    GeneratedImage,
    ImageInput,
    LLMConfigError,
    LLMError,
    LLMMessage,
    LLMOutputError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ModelProfile,
)
from app.ai.llm.limiter import ConcurrencyLimiter
from app.ai.llm.schema import extract_json, schema_prompt, to_llm_schema
from app.ai.llm.usage import UsageRecord, UsageSink
from app.core.logging import get_logger
from app.domain.enums import LLMCallStatus

T = TypeVar("T", bound=BaseModel)
log = get_logger(__name__)

_MAX_ERROR_CHARS = 1500


@dataclass(frozen=True, slots=True)
class CallContext:
    """К чему относится вызов: для учёта расхода и логов."""

    purpose: str
    project_id: UUID | None = None
    user_id: UUID | None = None


class LLMService:
    def __init__(
        self,
        provider: LLMProvider,
        *,
        models: Mapping[ModelProfile, str],
        limiter: ConcurrencyLimiter,
        usage: UsageSink,
    ) -> None:
        self._provider = provider
        self._models = dict(models)
        self._limiter = limiter
        self._usage = usage

    def model_for(self, profile: ModelProfile) -> str:
        return self._models.get(profile) or self._models[ModelProfile.SMART]

    @property
    def supports_images(self) -> bool:
        return self._provider.supports_images

    async def describe_image(
        self, image: ImageInput, prompt: str, *, ctx: CallContext, max_tokens: int = 1500
    ) -> str:
        """Текст и описание изображения от мультимодальной модели."""
        if not self._provider.supports_images:
            raise LLMConfigError("Текущая модель не умеет читать изображения")
        request = LLMRequest(
            model=self.model_for(ModelProfile.VISION),
            messages=[LLMMessage(role="user", content=prompt)],
            max_tokens=max_tokens,
            images=(image,),
        )
        response = await self._call(request, ctx)
        return response.text.strip()

    @property
    def supports_image_generation(self) -> bool:
        return getattr(self._provider, "supports_image_generation", False)

    async def generate_image(self, messages: list[LLMMessage], *, ctx: CallContext) -> GeneratedImage:
        """Картинка по описанию (у GigaChat — Kandinsky). Та же очередь и тот же учёт, что у текста."""
        if not self.supports_image_generation:
            raise LLMConfigError("Подключённая модель не умеет рисовать картинки")
        model = self.model_for(ModelProfile.SMART)
        started = time.monotonic()
        try:
            async with self._limiter.slot():
                started = time.monotonic()
                image = await self._provider.generate_image(model, messages)
        except LLMError as exc:
            await self._usage.record(
                UsageRecord(
                    purpose=ctx.purpose,
                    provider=self._provider.name,
                    model=model,
                    status=LLMCallStatus.ERROR,
                    latency_ms=_elapsed_ms(started),
                    project_id=ctx.project_id,
                    user_id=ctx.user_id,
                    error=f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS],
                )
            )
            raise
        await self._usage.record(
            UsageRecord(
                purpose=ctx.purpose,
                provider=self._provider.name,
                model=image.model or model,
                status=LLMCallStatus.OK,
                input_tokens=image.usage.input_tokens,
                output_tokens=image.usage.output_tokens,
                latency_ms=_elapsed_ms(started),
                project_id=ctx.project_id,
                user_id=ctx.user_id,
            )
        )
        return image

    async def text(
        self,
        messages: list[LLMMessage],
        *,
        ctx: CallContext,
        profile: ModelProfile = ModelProfile.SMART,
        max_tokens: int = 1024,
        temperature: float | None = None,
    ) -> str:
        request = LLMRequest(
            model=self.model_for(profile), messages=messages, max_tokens=max_tokens, temperature=temperature
        )
        response = await self._call(request, ctx)
        return response.text.strip()

    async def structured(
        self,
        messages: list[LLMMessage],
        schema: type[T],
        *,
        ctx: CallContext,
        profile: ModelProfile = ModelProfile.FAST,
        max_tokens: int = 1024,
        temperature: float | None = 0.1,
    ) -> T:
        """Ответ модели в виде проверенного объекта schema.

        Если JSON не прошёл проверку, модель получает текст ошибки и одну попытку
        исправиться. Повторная неудача — LLMOutputError.
        """
        json_schema = to_llm_schema(schema)
        conversation = list(messages)
        if not self._provider.supports_json_schema:
            conversation.append(
                LLMMessage(
                    role="system",
                    content=(
                        "Ответь только JSON-объектом без пояснений по этой JSON Schema: "
                        f"{schema_prompt(schema)}"
                    ),
                )
            )
        request = LLMRequest(
            model=self.model_for(profile),
            messages=conversation,
            max_tokens=max_tokens,
            temperature=temperature,
            json_schema=json_schema,
        )
        response = await self._call(request, ctx)
        try:
            return schema.model_validate_json(extract_json(response.text))
        except ValidationError as first_error:
            log.warning("llm.invalid_output", purpose=ctx.purpose, errors=first_error.error_count())
            await self._record_invalid(ctx, request.model, first_error)
            repair_request = LLMRequest(
                model=request.model,
                messages=[
                    *conversation,
                    LLMMessage(role="assistant", content=response.text[:4000]),
                    LLMMessage(
                        role="user",
                        content=(
                            "Ответ не прошёл проверку схемы. Ошибки: "
                            f"{_format_errors(first_error)}\nВерни исправленный JSON целиком, без пояснений."
                        ),
                    ),
                ],
                max_tokens=max_tokens,
                temperature=temperature,
                json_schema=json_schema,
            )
            repaired = await self._call(repair_request, ctx)
            try:
                return schema.model_validate_json(extract_json(repaired.text))
            except ValidationError as second_error:
                await self._record_invalid(ctx, request.model, second_error)
                raise LLMOutputError(
                    "Модель вернула ответ в неверном формате — попробуйте переформулировать запрос"
                ) from second_error

    async def _call(self, request: LLMRequest, ctx: CallContext) -> LLMResponse:
        """Вызов с учётом расхода. Пустой ответ модели повторяется один раз."""
        response = await self._call_once(request, ctx)
        if response.text.strip():
            return response
        log.warning("llm.empty_output", purpose=ctx.purpose, model=request.model)
        return await self._call_once(request, ctx)

    async def _call_once(self, request: LLMRequest, ctx: CallContext) -> LLMResponse:
        started = time.monotonic()
        try:
            async with self._limiter.slot():
                started = time.monotonic()  # время ожидания очереди не считаем задержкой модели
                response = await self._provider.complete(request)
        except LLMError as exc:
            await self._usage.record(
                UsageRecord(
                    purpose=ctx.purpose,
                    provider=self._provider.name,
                    model=request.model,
                    status=LLMCallStatus.ERROR,
                    latency_ms=_elapsed_ms(started),
                    project_id=ctx.project_id,
                    user_id=ctx.user_id,
                    error=f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS],
                )
            )
            raise
        await self._usage.record(
            UsageRecord(
                purpose=ctx.purpose,
                provider=self._provider.name,
                model=response.model or request.model,
                status=LLMCallStatus.OK,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                latency_ms=_elapsed_ms(started),
                project_id=ctx.project_id,
                user_id=ctx.user_id,
            )
        )
        return response

    async def _record_invalid(self, ctx: CallContext, model: str, error: ValidationError) -> None:
        await self._usage.record(
            UsageRecord(
                purpose=ctx.purpose,
                provider=self._provider.name,
                model=model,
                status=LLMCallStatus.INVALID_OUTPUT,
                project_id=ctx.project_id,
                user_id=ctx.user_id,
                error=_format_errors(error)[:_MAX_ERROR_CHARS],
            )
        )

    async def aclose(self) -> None:
        await self._provider.aclose()


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _format_errors(error: ValidationError) -> str:
    parts = []
    for item in error.errors()[:10]:
        location = ".".join(str(part) for part in item["loc"]) or "<корень>"
        parts.append(f"{location}: {item['msg']}")
    return "; ".join(parts)
