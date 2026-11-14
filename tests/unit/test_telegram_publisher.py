"""Публикатор Telegram: классификация ошибок (повторять или спросить владельца), проверка канала."""

from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import ChatMemberAdministrator, ChatMemberMember, ChatMemberOwner
from aiogram.types import User as TgUser
from aiohttp import ClientConnectorError
from aiohttp.client_reqrep import ConnectionKey

from app.bot.channels import channel_reference
from app.integrations.social.base import PostContent, PublishError, PublishErrorKind
from app.integrations.social.telegram import ChannelCheckError, TelegramPublisher, post_url
from tests.fakes import METHOD, FakeBot


def connect_error() -> TelegramNetworkError:
    key = ConnectionKey("api.telegram.org", 443, True, True, None, None, None)
    error = TelegramNetworkError(method=METHOD, message="ClientConnectorError")
    error.__cause__ = ClientConnectorError(key, OSError(61, "refused"))
    return error


def timeout_error() -> TelegramNetworkError:
    error = TelegramNetworkError(method=METHOD, message="Request timeout error")
    error.__cause__ = TimeoutError()
    return error


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (
            TelegramRetryAfter(method=METHOD, message="Too Many Requests", retry_after=3),
            PublishErrorKind.RETRYABLE,
        ),
        (connect_error(), PublishErrorKind.RETRYABLE),  # соединения не было — запрос до Telegram не дошёл
        (timeout_error(), PublishErrorKind.UNKNOWN),  # запрос ушёл, ответа нет — пост мог выйти
        (TelegramServerError(method=METHOD, message="Bad Gateway"), PublishErrorKind.UNKNOWN),
        (TelegramBadRequest(method=METHOD, message="chat not found"), PublishErrorKind.PERMANENT),
        (TelegramForbiddenError(method=METHOD, message="bot is not a member"), PublishErrorKind.PERMANENT),
    ],
)
async def test_errors_are_classified(error: Exception, kind: PublishErrorKind) -> None:
    publisher = TelegramPublisher(FakeBot(errors=[error]))  # type: ignore[arg-type]
    with pytest.raises(PublishError) as caught:
        await publisher.publish("-1001234", PostContent(text="<b>Раф</b>"))
    assert caught.value.kind is kind


async def test_publish_text_and_photo() -> None:
    bot = FakeBot()
    publisher = TelegramPublisher(bot)  # type: ignore[arg-type]
    result = await publisher.publish("-1001234", PostContent(text="<b>Раф</b>"))
    assert result.url == f"https://t.me/zerno_kzn/{result.external_id}"
    await publisher.publish("-1001234", PostContent(text="<b>Раф</b>", image=b"jpeg"))
    assert [m["kind"] for m in bot.sent] == ["text", "photo"]
    assert bot.sent[1]["caption"] == "<b>Раф</b>" and bot.sent[0]["chat_id"] == -1001234


def test_post_url_for_public_and_private_channels() -> None:
    assert post_url(SimpleNamespace(username="cafe", id=-1001), 5) == "https://t.me/cafe/5"  # type: ignore[arg-type]
    assert post_url(SimpleNamespace(username=None, id=-1009876), 7) == "https://t.me/c/9876/7"  # type: ignore[arg-type]


def test_validate_content_uses_caption_limit_with_image() -> None:
    publisher = TelegramPublisher(FakeBot())  # type: ignore[arg-type]
    assert publisher.validate_content(PostContent(text="я" * 1100)) == []
    assert publisher.validate_content(PostContent(text="я" * 1100, image=b"x"))


def tg_user(user_id: int, *, is_bot: bool = False) -> TgUser:
    return TgUser(id=user_id, is_bot=is_bot, first_name="x")


class ChannelBot(FakeBot):
    def __init__(self, members: dict[int, Any], chat_type: str = "channel") -> None:
        super().__init__()
        self.members = members
        self.chat_type = chat_type

    async def get_chat(self, chat_ref: Any) -> SimpleNamespace:
        return SimpleNamespace(id=-100555, type=self.chat_type, title="Зерно · Казань", username="zerno_kzn")

    async def get_chat_member(self, chat_id: int, user_id: int) -> Any:
        if user_id not in self.members:
            raise TelegramBadRequest(method=METHOD, message="user not found")
        return self.members[user_id]

    async def me(self) -> TgUser:
        return tg_user(999, is_bot=True)


def admin(user_id: int, *, can_post: bool) -> ChatMemberAdministrator:
    required = [
        name
        for name, field in ChatMemberAdministrator.model_fields.items()
        if field.is_required() and name != "user"
    ]
    flags: dict[str, Any] = dict.fromkeys(required, False)
    return ChatMemberAdministrator(user=tg_user(user_id), can_post_messages=can_post, **flags)


async def test_channel_check_requires_bot_rights_and_requester_admin() -> None:
    owner = ChatMemberOwner(user=tg_user(1), is_anonymous=False)
    bot: Any = ChannelBot({1: owner, 999: admin(999, can_post=True)})
    ok = await TelegramPublisher(bot).check_channel("@zerno_kzn", requested_by=1)
    assert ok.can_post and ok.chat_id == -100555 and ok.problems == []

    bot = ChannelBot({1: owner, 999: admin(999, can_post=False)})
    no_rights = await TelegramPublisher(bot).check_channel("@zerno_kzn", requested_by=1)
    assert not no_rights.can_post and "Публикация сообщений" in no_rights.problems[0]

    stranger = ChannelBot({2: ChatMemberMember(user=tg_user(2)), 999: admin(999, can_post=True)})
    with pytest.raises(ChannelCheckError, match="только его администратор"):
        await TelegramPublisher(stranger).check_channel("@zerno_kzn", requested_by=2)  # type: ignore[arg-type]

    with pytest.raises(ChannelCheckError, match="не канал"):
        await TelegramPublisher(ChannelBot({}, chat_type="supergroup")).check_channel("@x", requested_by=1)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("@zerno_kzn", "@zerno_kzn"),
        ("https://t.me/zerno_kzn", "@zerno_kzn"),
        ("t.me/zerno_kzn/", "@zerno_kzn"),
        ("мой канал @zerno_kzn", None),  # не только адрес — обычный текст
        ("https://zerno.ru", None),
    ],
)
def test_channel_reference(text: str, expected: str | None) -> None:
    assert channel_reference(text) == expected
