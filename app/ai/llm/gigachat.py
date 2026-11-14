"""Провайдер GigaChat (Сбер) на официальном SDK.

Особенности:
- нужен корневой сертификат Минцифры — путь к бандлу передаётся только этому клиенту;
- токен доступа живёт ~30 минут, SDK обновляет его сам;
- на тарифе физлица разрешён один одновременный запрос — очередь обеспечивает
  ConcurrencyLimiter в LLMService, а не провайдер;
- картинки рисует встроенная функция text2image (Kandinsky): запрос в чат с
  function_call="auto", в ответе — тег <img src="file_id">, файл скачивается отдельно
  (проверено 14.11.2026 на GigaChat-2-Max и GigaChat-3-Ultra, ~20–35 с).
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager, suppress

import httpx
import pydantic
from gigachat import GigaChatAsyncClient
from gigachat import exceptions as gc_errors
from gigachat.models import Chat, Messages, MessagesRole

from app.ai.llm.base import (
    GeneratedImage,
    LLMConfigError,
    LLMMessage,
    LLMOutputError,
    LLMRateLimitError,
    LLMRequest,
    LLMResponse,
    LLMUnavailableError,
    LLMUsageInfo,
)
from app.core.config import Settings

_ROLES = {"system": MessagesRole.SYSTEM, "user": MessagesRole.USER, "assistant": MessagesRole.ASSISTANT}
_IMG_TAG = re.compile(r'<img\s+src="([^"]+)"')


class GigaChatProvider:
    name = "gigachat"
    supports_json_schema = True
    supports_images = True  # через загрузку файла и attachments (проверено на тарифе физлица)
    supports_image_generation = True

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
        uploaded: list[str] = []
        try:
            for image in request.images:
                with _mapped_errors():
                    file = await self._client.aupload_file((image.filename, image.data, image.mime_type))
                uploaded.append(file.id_)
            messages = _to_gigachat_messages(request.messages)
            if uploaded:
                last_user = max(i for i, m in enumerate(messages) if m.role == MessagesRole.USER)
                messages[last_user].attachments = uploaded
            chat = Chat(
                model=request.model,
                messages=messages,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                response_format=(
                    {"type": "json_schema", "schema": request.json_schema, "strict": False}
                    if request.json_schema is not None
                    else None
                ),
            )
            with _mapped_errors():
                completion = await self._client.achat(chat)
        except pydantic.ValidationError:
            # API иногда присылает пустое сообщение без role, и SDK не может его разобрать.
            # Отдаём пустой ответ: LLMService повторит запрос.
            return LLMResponse(text="", model=request.model, finish_reason="empty")
        finally:
            for file_id in uploaded:
                # Файлы во внешнем хранилище не оставляем; неудача удаления не должна ломать ответ.
                with suppress(gc_errors.GigaChatException, httpx.HTTPError):
                    await self._client.adelete_file(file_id)

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

    async def generate_image(self, model: str, messages: list[LLMMessage]) -> GeneratedImage:
        try:
            with _mapped_errors():
                completion = await self._client.achat(
                    Chat(model=model, messages=_to_gigachat_messages(messages), function_call="auto")
                )
                content = completion.choices[0].message.content or ""
                match = _IMG_TAG.search(content)
                if match is None:
                    # Модель ответила текстом (например, отказалась рисовать по теме).
                    raise LLMOutputError("GigaChat не нарисовал картинку — попробуйте описать сюжет иначе")
                image = await self._client.aget_image(match.group(1))
            data = base64.b64decode(image.content, validate=True)
        except (pydantic.ValidationError, binascii.Error) as exc:
            raise LLMOutputError("GigaChat вернул повреждённую картинку — попробуйте ещё раз") from exc
        return GeneratedImage(
            data=data,
            mime_type=_image_mime(data),
            model=completion.model,
            usage=LLMUsageInfo(
                input_tokens=completion.usage.prompt_tokens,
                output_tokens=completion.usage.completion_tokens,
            ),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


@contextmanager
def _mapped_errors() -> Iterator[None]:
    """Ошибки SDK и сети → ошибки приложения с понятным текстом."""
    try:
        yield
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


def _image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


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
