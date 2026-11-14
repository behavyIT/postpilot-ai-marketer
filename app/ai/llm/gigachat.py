"""Провайдер GigaChat (Сбер) на официальном SDK.

Особенности:
- нужен корневой сертификат Минцифры — путь к бандлу передаётся только этому клиенту;
- токен доступа живёт ~30 минут, SDK обновляет его сам;
- на тарифе физлица разрешён один одновременный запрос — очередь обеспечивает
  ConcurrencyLimiter в LLMService, а не провайдер.
"""

from __future__ import annotations

import json

import httpx
import pydantic
from gigachat import GigaChatAsyncClient
from gigachat import exceptions as gc_errors
from gigachat.models import Chat, Messages, MessagesRole

from app.ai.llm.base import (
    LLMConfigError,
    LLMMessage,
    LLMRateLimitError,
    LLMRequest,
    LLMResponse,
    LLMUnavailableError,
    LLMUsageInfo,
)
from app.core.config import Settings

_ROLES = {"system": MessagesRole.SYSTEM, "user": MessagesRole.USER, "assistant": MessagesRole.ASSISTANT}


class GigaChatProvider:
    name = "gigachat"
    supports_json_schema = True

    def __init__(self, settings: Settings) -> None:
        if settings.gigachat_auth_key is None:
            raise LLMConfigError("GIGACHAT_AUTH_KEY не задан в .env")
        self._client = GigaChatAsyncClient(
            credentials=settings.gigachat_auth_key.get_secret_value(),
            scope=settings.gigachat_scope,
            base_url=settings.gigachat_base_url,
            ca_bundle_file=str(settings.gigachat_ca_bundle),
            timeout=settings.gigachat_timeout_s,
            verify_ssl_certs=True,
            max_retries=2,
            retry_backoff_factor=1.0,
        )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        chat = Chat(
            model=request.model,
            messages=_to_gigachat_messages(request.messages),
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            response_format=(
                {"type": "json_schema", "schema": request.json_schema, "strict": False}
                if request.json_schema is not None
                else None
            ),
        )
        try:
            completion = await self._client.achat(chat)
        except gc_errors.RateLimitError as exc:
            raise LLMRateLimitError("GigaChat ограничил частоту запросов, попробуйте через минуту") from exc
        except (gc_errors.AuthenticationError, gc_errors.ForbiddenError) as exc:
            raise LLMConfigError("GigaChat не принял ключ: проверьте GIGACHAT_AUTH_KEY") from exc
        except (
            gc_errors.BadRequestError,
            gc_errors.NotFoundError,
            gc_errors.UnprocessableEntityError,
        ) as exc:
            raise LLMConfigError(f"GigaChat отклонил запрос: {_short_error(exc)}") from exc
        except (gc_errors.ServerError, httpx.TransportError) as exc:
            raise LLMUnavailableError("GigaChat временно недоступен, попробуйте позже") from exc
        except pydantic.ValidationError:
            # API иногда присылает пустое сообщение без role, и SDK не может его разобрать.
            # Отдаём пустой ответ: LLMService повторит запрос.
            return LLMResponse(text="", model=request.model, finish_reason="empty")

        choice = completion.choices[0]
        return LLMResponse(
            text=choice.message.content or "",
            model=completion.model,
            finish_reason=choice.finish_reason,
            usage=LLMUsageInfo(
                input_tokens=completion.usage.prompt_tokens,
                output_tokens=completion.usage.completion_tokens,
            ),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _to_gigachat_messages(messages: list[LLMMessage]) -> list[Messages]:
    """GigaChat принимает одно системное сообщение, и только первым.

    Все системные инструкции склеиваются в одно сообщение в начале диалога,
    остальные реплики сохраняют порядок.
    """
    system_parts = [m.content for m in messages if m.role == "system"]
    result: list[Messages] = []
    if system_parts:
        result.append(Messages(role=MessagesRole.SYSTEM, content="\n\n".join(system_parts)))
    result.extend(Messages(role=_ROLES[m.role], content=m.content) for m in messages if m.role != "system")
    return result


def _short_error(exc: gc_errors.ResponseError) -> str:
    """Код и сообщение API без URL и заголовков."""
    message = ""
    if exc.content:
        try:
            payload = json.loads(exc.content)
            message = str(payload.get("message") or "")
        except (ValueError, AttributeError):
            message = exc.content.decode("utf-8", errors="replace")[:200]
    return f"{exc.status_code} {message}".strip()
