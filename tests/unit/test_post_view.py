"""Карточки постов: кнопки по статусу, варианты времени, сообщения об итоге публикации."""

import base64
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agents.orchestrator.post_view import (
    ImageData,
    channel_preview,
    outcome_reply,
    post_card,
    quick_time,
    schedule_options,
)
from app.agents.orchestrator.replies import Action
from app.agents.visual import ImageBrief, build_image_prompt
from app.ai.llm.base import LLMMessage, LLMOutputError
from app.ai.llm.gigachat import GigaChatProvider
from app.db.models import Publication, PublicationVariant, PublicationVersion
from app.domain.enums import PublicationStatus as S
from app.services.publications import PostView
from app.services.publishing import OutcomeKind, PublishOutcome

NOW = datetime(2026, 11, 14, 12, 20, tzinfo=UTC)  # 15:20 по Москве


def view(
    status: S, *, planned_in: timedelta | None = timedelta(days=1), warnings: list[str] | None = None
) -> PostView:
    publication = Publication(
        id=uuid4(),
        project_id=uuid4(),
        planned_at=NOW + planned_in if planned_in else None,
        category="Полезное",
        goal="education",
        topic="Зерно",
        title="Как выбрать зерно",
        brief="",
        visual_idea="Зёрна крупным планом",
    )
    variant = PublicationVariant(
        id=uuid4(), publication_id=publication.id, platform="telegram", status=status
    )
    variant.current_version_no = 2
    version = PublicationVersion(
        variant_id=variant.id, version_no=2, body="<b>Зерно</b>\nТекст", warnings=warnings or []
    )
    return PostView(publication=publication, variant=variant, version=version, media=None, account=None)


def actions(reply) -> list[Action]:
    return [b.action for row in reply.buttons for b in row]


def test_pending_card_has_approval_buttons_and_exact_text() -> None:
    reply = post_card(
        view(S.PENDING_APPROVAL, warnings=["Цифра «20%» не найдена"]), timezone="Europe/Moscow", image=None
    )
    assert {
        Action.POST_APPROVE,
        Action.POST_EDIT,
        Action.POST_REDO,
        Action.POST_IMAGE,
        Action.POST_REJECT,
    } <= set(actions(reply))
    assert Action.POST_NOW not in actions(reply)  # без одобрения публиковать нельзя
    assert "<b>Зерно</b>\nТекст" in reply.text and "«20%»" in reply.text
    assert reply.post_ref is not None and reply.post_ref.version_no == 2
    assert all(b.value.endswith(".2") for row in reply.buttons for b in row)  # кнопки знают версию


def test_buttons_follow_status() -> None:
    assert Action.POST_WHEN in actions(post_card(view(S.APPROVED), timezone="Europe/Moscow", image=None))
    scheduled = actions(post_card(view(S.SCHEDULED), timezone="Europe/Moscow", image=None))
    assert Action.POST_UNSCHEDULE in scheduled and Action.POST_APPROVE not in scheduled
    assert Action.POST_RETRY in actions(post_card(view(S.FAILED), timezone="Europe/Moscow", image=None))
    assert actions(post_card(view(S.CANCELLED), timezone="Europe/Moscow", image=None)) == []


def test_image_card_and_channel_preview() -> None:
    reply = post_card(view(S.PENDING_APPROVAL), timezone="Europe/Moscow", image=ImageData(b"jpeg", "a.jpg"))
    assert reply.image == b"jpeg" and Action.POST_NO_IMAGE in actions(reply)
    assert "Идея картинки" not in reply.text
    preview = channel_preview(view(S.APPROVED), ImageData(b"jpeg", "a.jpg"))
    assert preview.text == "<b>Зерно</b>\nТекст" and preview.buttons == []


def test_schedule_options() -> None:
    rows = schedule_options(view(S.APPROVED), timezone="Europe/Moscow", now=NOW)
    values = [b.value.rsplit(".", 1)[-1] for row in rows for b in row if b.action is Action.POST_AT]
    assert values == ["plan", "1h", "t19", "m10", "m19", "custom"]
    late = NOW.replace(hour=17)  # 20:00 по Москве — «сегодня в 19:00» уже не предлагаем
    rows = schedule_options(view(S.APPROVED, planned_in=None), timezone="Europe/Moscow", now=late)
    values = [b.value.rsplit(".", 1)[-1] for row in rows for b in row if b.action is Action.POST_AT]
    assert "t19" not in values and "plan" not in values


def test_quick_time() -> None:
    local = NOW.astimezone(__import__("zoneinfo").ZoneInfo("Europe/Moscow"))
    tomorrow_10 = quick_time("m10", local)
    assert tomorrow_10 is not None and (tomorrow_10.day, tomorrow_10.hour) == (15, 10)
    assert quick_time("нет такого", local) is None


def outcome(kind: OutcomeKind, **kwargs: str) -> PublishOutcome:
    return PublishOutcome(kind, uuid4(), title="Раф", notify=[1], **kwargs)  # type: ignore[arg-type]


def test_outcome_replies() -> None:
    published = outcome_reply(outcome(OutcomeKind.PUBLISHED, url="https://t.me/x/5", channel_title="Зерно"))
    assert published is not None and "Опубликовано в «Зерно»" in published.text
    assert published.buttons[0][0].url == "https://t.me/x/5"
    unknown = outcome_reply(outcome(OutcomeKind.UNKNOWN, reason="Telegram не ответил"))
    assert unknown is not None and "Проверьте канал" in unknown.text and "дубля" in unknown.text
    assert outcome_reply(outcome(OutcomeKind.SKIPPED)) is None


def test_image_prompt_forbids_text_and_uses_request() -> None:
    prompt = build_image_prompt(
        ImageBrief(topic="Раф", visual_idea="Чашка на окне", industry="кофейня", request="с котом")
    )
    assert "с котом" in prompt and "Чашка на окне" in prompt and "Без текста" in prompt


class FakeGigaClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.downloaded: list[str] = []

    async def achat(self, chat):
        assert chat.function_call == "auto"
        message = SimpleNamespace(content=self.content)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            model="GigaChat-2-Max",
            usage=SimpleNamespace(prompt_tokens=1400, completion_tokens=60),
        )

    async def aget_image(self, file_id: str):
        self.downloaded.append(file_id)
        return SimpleNamespace(content=base64.b64encode(b"\xff\xd8\xff\xe0jpeg").decode())


def provider_with(client: FakeGigaClient) -> GigaChatProvider:
    provider = GigaChatProvider.__new__(GigaChatProvider)
    provider._client = client  # type: ignore[assignment]
    return provider


async def test_gigachat_image_is_downloaded_by_file_id() -> None:
    client = FakeGigaClient('<img src="b3b42c61-9a40" fuse="true"/> вот картинка')
    image = await provider_with(client).generate_image(
        "GigaChat-2-Max", [LLMMessage(role="user", content="кот")]
    )
    assert client.downloaded == ["b3b42c61-9a40"]
    assert image.mime_type == "image/jpeg" and image.data.startswith(b"\xff\xd8")
    assert image.usage.input_tokens == 1400


async def test_gigachat_text_answer_instead_of_image_is_an_error() -> None:
    client = FakeGigaClient("Не могу нарисовать это")
    with pytest.raises(LLMOutputError, match="не нарисовал"):
        await provider_with(client).generate_image("GigaChat-2-Max", [LLMMessage(role="user", content="кот")])
