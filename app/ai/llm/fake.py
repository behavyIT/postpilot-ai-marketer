"""Провайдер для тестов: возвращает заранее заданные ответы и запоминает запросы."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from app.ai.llm.base import LLMError, LLMRequest, LLMResponse, LLMUsageInfo

Responder = Callable[[LLMRequest], str | dict[str, Any] | Exception]


class FakeLLMProvider:
    name = "fake"

    def __init__(
        self,
        *responses: str | dict[str, Any] | Exception,
        responder: Responder | None = None,
        supports_json_schema: bool = True,
    ) -> None:
        self._responses = list(responses)
        self._responder = responder
        self.supports_json_schema = supports_json_schema
        self.requests: list[LLMRequest] = []

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

    async def aclose(self) -> None:
        return None
