from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendMessage

from app.agents.orchestrator.replies import Reply
from app.bot.notifier import TelegramNotifier
from app.bot.render import as_plain, send_with_fallback

METHOD = SendMessage(chat_id=1, text="x")


async def test_html_rejected_by_telegram_is_resent_as_plain_text() -> None:
    calls: list[dict[str, Any]] = []

    async def send(text: str, **kwargs: Any) -> None:
        calls.append({"text": text, **kwargs})
        if len(calls) == 1:
            raise TelegramBadRequest(method=METHOD, message="Bad Request: can't parse entities")

    await send_with_fallback(send, Reply(text="<b>Раф</b>: 5 &lt; 10"))

    assert calls[0]["text"] == "<b>Раф</b>: 5 &lt; 10"
    assert calls[1] == {"text": "Раф: 5 < 10", "reply_markup": None, "parse_mode": None}


async def test_other_bad_requests_are_not_swallowed() -> None:
    async def send(text: str, **kwargs: Any) -> None:
        raise TelegramBadRequest(method=METHOD, message="Bad Request: chat not found")

    with pytest.raises(TelegramBadRequest):
        await send_with_fallback(send, Reply(text="x"))


class FlakyBot:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.sent: list[str] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> None:
        if self.failures:
            self.failures -= 1
            raise TelegramNetworkError(method=METHOD, message="Cannot connect to host api.telegram.org")
        self.sent.append(text)


async def test_notifier_retries_network_errors() -> None:
    bot = FlakyBot(failures=2)
    await TelegramNotifier(bot, base_delay_s=0).send(1, Reply(text="Готово"))  # type: ignore[arg-type]
    assert bot.sent == ["Готово"]


async def test_notifier_gives_up_after_limit() -> None:
    with pytest.raises(TelegramNetworkError):
        await TelegramNotifier(FlakyBot(failures=10), base_delay_s=0).send(1, Reply(text="x"))  # type: ignore[arg-type]


def test_plain_text_is_cut_to_telegram_limit() -> None:
    assert len(as_plain("<b>" + "я" * 5000 + "</b>")) == 4096
