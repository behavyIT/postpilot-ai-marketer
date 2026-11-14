"""Спайк: проверка ключа GigaChat и возможностей API на тарифе физлица.

Запуск: uv run python scripts/spike_gigachat.py

Проверяет по шагам: получение токена, список моделей, ответы моделей smart и fast,
структурированный вывод (JSON Schema) и вызов функций. Секреты не печатаются.
Тратит несколько тысяч токенов из бесплатной квоты.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigachat import GigaChat
from gigachat.models import Chat, Function, FunctionParameters, Messages, MessagesRole
from pydantic import BaseModel, Field

from app.core.config import get_settings


class PostIdea(BaseModel):
    title: str = Field(description="Заголовок поста, до 10 слов")
    hook: str = Field(description="Первая фраза поста, цепляющая внимание")
    hashtags: list[str] = Field(description="От 2 до 3 хештегов")


SCHEDULE_FUNCTION = Function(
    name="schedule_post",
    description="Запланировать публикацию поста на дату и время",
    parameters=FunctionParameters(
        type="object",
        properties={
            "topic": {"type": "string", "description": "Тема поста"},
            "date": {"type": "string", "description": "Дата в формате YYYY-MM-DD"},
            "time": {"type": "string", "description": "Время в формате HH:MM"},
        },
        required=["topic", "date", "time"],
    ),
)

results: list[tuple[str, bool, str]] = []


def step(name: str, action: Callable[[], str]) -> None:
    started = time.monotonic()
    try:
        detail = action()
        ok = True
    except Exception as exc:
        detail = f"{type(exc).__name__}: {str(exc)[:300]}"
        ok = False
    elapsed = time.monotonic() - started
    results.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name} ({elapsed:.1f} с)\n   {detail}\n", flush=True)


def _user(text: str) -> list[Messages]:
    return [Messages(role=MessagesRole.USER, content=text)]


def _usage(completion: Any) -> str:
    usage = getattr(completion, "usage", None)
    if usage is None:
        return "usage: нет данных"
    return f"токены: вход {usage.prompt_tokens}, выход {usage.completion_tokens}"


def main() -> int:
    settings = get_settings()
    if settings.gigachat_auth_key is None:
        print("GIGACHAT_AUTH_KEY не задан: впишите Authorization key в файл .env")
        return 2

    client = GigaChat(
        credentials=settings.gigachat_auth_key.get_secret_value(),
        scope=settings.gigachat_scope,
        base_url=settings.gigachat_base_url,
        ca_bundle_file=str(settings.gigachat_ca_bundle),
        timeout=settings.gigachat_timeout_s,
        verify_ssl_certs=True,
    )
    smart, fast = settings.gigachat_model_smart, settings.gigachat_model_fast

    def get_token() -> str:
        token = client.get_token()
        if token is None:
            raise RuntimeError("Токен не получен")
        return "токен получен (действует ~30 минут)"

    def list_models() -> str:
        ids = sorted(model.id_ for model in client.get_models().data)
        missing = [name for name in (smart, fast) if name not in ids]
        note = f"; нет в списке: {missing}" if missing else ""
        return f"доступно {len(ids)}: {', '.join(ids)}{note}"

    def simple_chat(model: str) -> Callable[[], str]:
        def run() -> str:
            completion = client.chat(
                Chat(
                    model=model,
                    messages=_user(
                        "Придумай заголовок поста для кофейни про осенний раф. Не больше 10 слов."
                    ),
                    max_tokens=60,
                )
            )
            text = completion.choices[0].message.content.strip()
            return f"«{text}» | {_usage(completion)}"

        return run

    def structured(model: str) -> Callable[[], str]:
        def run() -> str:
            completion, parsed = client.chat_parse(
                Chat(
                    model=model,
                    messages=_user("Предложи идею поста для кофейни про осенний раф с корицей."),
                ),
                response_format=PostIdea,
            )
            return f"{parsed.model_dump_json()} | {_usage(completion)}"

        return run

    def function_call() -> str:
        completion = client.chat(
            Chat(
                model=smart,
                messages=_user("Запланируй пост про осенний раф на 20 ноября 2026 года в 12:00."),
                functions=[SCHEDULE_FUNCTION],
                function_call="auto",
            )
        )
        choice = completion.choices[0]
        call = choice.message.function_call
        if choice.finish_reason != "function_call" or call is None:
            raise RuntimeError(f"Модель не вызвала функцию: finish_reason={choice.finish_reason}")
        return f"{call.name}({call.arguments}) | {_usage(completion)}"

    step("Токен доступа (OAuth)", get_token)
    step("Список моделей", list_models)
    step(f"Ответ модели fast ({fast})", simple_chat(fast))
    step(f"Ответ модели smart ({smart})", simple_chat(smart))
    step(f"Структурированный вывод, fast ({fast})", structured(fast))
    step(f"Структурированный вывод, smart ({smart})", structured(smart))
    step(f"Вызов функции, smart ({smart})", function_call)

    client.close()
    passed = sum(ok for _, ok, _ in results)
    print(f"Итог: {passed} из {len(results)} проверок прошли.")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
