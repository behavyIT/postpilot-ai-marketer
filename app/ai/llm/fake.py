"""Провайдер для тестов: возвращает заранее заданные ответы и запоминает запросы."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from app.ai.llm.base import GeneratedImage, LLMError, LLMMessage, LLMRequest, LLMResponse, LLMUsageInfo

# Минимальный валидный JPEG-заголовок: тестам достаточно сигнатуры.
FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64

Responder = Callable[[LLMRequest], str | dict[str, Any] | Exception]


class FakeLLMProvider:
    name = "fake"

    def __init__(
        self,
        *responses: str | dict[str, Any] | Exception,
        responder: Responder | None = None,
        supports_json_schema: bool = True,
        supports_images: bool = True,
        image: bytes | Exception | None = FAKE_JPEG,
    ) -> None:
        self._responses = list(responses)
        self._responder = responder
        self.supports_json_schema = supports_json_schema
        self.supports_images = supports_images
        self.supports_image_generation = image is not None
        self._image = image
        self.requests: list[LLMRequest] = []
        self.image_requests: list[list[LLMMessage]] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if self._responder is not None:
            answer = self._responder(request)
        elif self._responses:
            answer = self._responses.pop(0)
        else:
            raise LLMError("FakeLLMProvider: ответы закончились")
        if isinstance(answer, Exception):
            raise answer
        text = json.dumps(answer, ensure_ascii=False) if isinstance(answer, dict) else answer
        return LLMResponse(
            text=text, model=request.model, usage=LLMUsageInfo(input_tokens=10, output_tokens=5)
        )

    async def generate_image(self, model: str, messages: list[LLMMessage]) -> GeneratedImage:
        self.image_requests.append(messages)
        if isinstance(self._image, Exception):
            raise self._image
        if self._image is None:
            raise LLMError("FakeLLMProvider: рисование отключено")
        return GeneratedImage(
            data=self._image, mime_type="image/jpeg", model=model, usage=LLMUsageInfo(10, 5)
        )

    async def aclose(self) -> None:
        return None
