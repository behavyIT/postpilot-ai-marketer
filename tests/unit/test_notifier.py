import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError

from app.agents.orchestrator.replies import Button, Reply
from app.bot.notifier import TelegramNotifier
from app.bot.render import as_plain, deliver, deliver_all
from tests.fakes import METHOD, FakeBot


async def test_html_rejected_by_telegram_is_resent_as_plain_text() -> None:
    bot = FakeBot(errors=[TelegramBadRequest(method=METHOD, message="Bad Request: can't parse entities")])
    ids = await deliver(bot, 1, Reply(text="<b>Раф</b>: 5 &lt; 10"))  # type: ignore[arg-type]
    assert len(ids) == 1
    assert bot.sent[0]["text"] == "Раф: 5 < 10" and bot.sent[0]["parse_mode"] is None


async def test_other_bad_requests_are_not_swallowed() -> None:
    bot = FakeBot(errors=[TelegramBadRequest(method=METHOD, message="Bad Request: chat not found")])
    with pytest.raises(TelegramBadRequest):
        await deliver(bot, 1, Reply(text="x"))  # type: ignore[arg-type]


async def test_post_with_image_goes_as_photo_with_caption_and_buttons() -> None:
    bot = FakeBot()
    reply = Reply(text="<b>Раф</b>", image=b"jpeg", buttons=[[Button(text="✅", value="x")]])
    ids = await deliver(bot, 5, reply)  # type: ignore[arg-type]
    assert len(ids) == 1
    assert bot.sent[0]["kind"] == "photo" and bot.sent[0]["caption"] == "<b>Раф</b>"
    assert bot.sent[0]["reply_markup"] is not None


async def test_long_caption_is_split_into_photo_and_text() -> None:
    bot = FakeBot()
    ids = await deliver(bot, 5, Reply(text="я" * 1500, image=b"jpeg"))  # type: ignore[arg-type]
    assert [m["kind"] for m in bot.sent] == ["photo", "text"]
    assert len(ids) == 2


async def test_followups_are_sent_and_reported() -> None:
    bot = FakeBot()
    seen: list[tuple[str, list[int]]] = []

    async def on_sent(part: Reply, ids: list[int]) -> None:
        seen.append((part.text, ids))

    await deliver_all(bot, 1, Reply(text="a", followups=[Reply(text="b")]), on_sent)  # type: ignore[arg-type]
    assert [text for text, _ in seen] == ["a", "b"]


async def test_url_buttons_are_links() -> None:
    from app.bot.keyboards import build_keyboard

    keyboard = build_keyboard(Reply(text="x", buttons=[[Button(text="Открыть", url="https://t.me/x/1")]]))
    assert keyboard is not None
    assert keyboard.inline_keyboard[0][0].url == "https://t.me/x/1"


async def test_notifier_retries_network_errors() -> None:
    errors: list[Exception] = [
        TelegramNetworkError(method=METHOD, message="Cannot connect") for _ in range(2)
    ]
    bot = FakeBot(errors=errors)
    ids = await TelegramNotifier(bot, base_delay_s=0).send(1, Reply(text="Готово"))  # type: ignore[arg-type]
    assert [m["text"] for m in bot.sent] == ["Готово"] and len(ids) == 1


async def test_notifier_gives_up_after_limit() -> None:
    errors: list[Exception] = [
        TelegramNetworkError(method=METHOD, message="Cannot connect") for _ in range(10)
    ]
    with pytest.raises(TelegramNetworkError):
        await TelegramNotifier(FakeBot(errors=errors), base_delay_s=0).send(1, Reply(text="x"))  # type: ignore[arg-type]


def test_plain_text_is_cut_to_telegram_limit() -> None:
    assert len(as_plain("<b>" + "я" * 5000 + "</b>")) == 4096
