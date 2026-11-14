"""Сценарии постов в чате: кнопки согласования, правки ответом на пост, расписание словами, канал."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.orchestrator.intents import Intent
from app.agents.orchestrator.orchestrator import Orchestrator, Turn
from app.agents.orchestrator.replies import Action, Expect, Reply
from app.ai.llm.fake import FakeLLMProvider
from app.bot.channels import connect_channel
from app.core.config import Settings
from app.db.models import Publication, PublicationVariant, PublicationVersion, SocialAccount
from app.domain.enums import ActorType, Platform, PublicationStatus, SocialAccountStatus
from app.domain.publications.refs import PostRef
from app.services.jobs import ContentJobKind, MemoryJobQueue, PublishJob
from app.services.publications import PublicationService
from tests.integration.helpers import intent, make_orchestrator, make_user, turn_for
from tests.unit.test_telegram_publisher import ChannelBot, admin

pytestmark = pytest.mark.integration
S = PublicationStatus
MSK = ZoneInfo("Europe/Moscow")


async def setup(
    session: AsyncSession,
    settings: Settings,
    telegram_id: int,
    *answers: dict[str, object],
    channel: bool = True,
) -> tuple[Turn, Orchestrator, MemoryJobQueue, PostRef]:
    user = await make_user(session, telegram_id)
    jobs = MemoryJobQueue()
    orchestrator = make_orchestrator(session, settings, FakeLLMProvider(*answers), jobs)
    turn = await turn_for(session, user)
    await orchestrator.create_project(turn, "Зерно")
    assert turn.project is not None
    publication = Publication(
        project_id=turn.project.id,
        planned_at=datetime.now(UTC) + timedelta(days=2),
        category="Полезное",
        goal="education",
        topic="Раф",
        title="Осенний раф",
        brief="",
    )
    session.add(publication)
    await session.flush()
    variant = PublicationVariant(publication_id=publication.id, platform=Platform.TELEGRAM, status=S.DRAFT)
    session.add(variant)
    await session.flush()
    await PublicationService(session).add_version(
        variant, body="<b>Раф</b>\nТекст", hashtags=[], actor=ActorType.AI
    )
    if channel:
        session.add(
            SocialAccount(
                project_id=turn.project.id,
                platform=Platform.TELEGRAM,
                external_id="-1001234",
                title="Зерно · Казань",
                status=SocialAccountStatus.ACTIVE,
                can_post=True,
            )
        )
        await session.flush()
    return turn, orchestrator, jobs, PostRef(variant.id, 1)


async def status_of(session: AsyncSession, ref: PostRef) -> str:
    variant = await session.get(PublicationVariant, ref.variant_id)
    assert variant is not None
    await session.refresh(variant)
    return variant.status


async def test_approve_then_schedule_tomorrow_morning(session: AsyncSession, test_settings: Settings) -> None:
    turn, orchestrator, jobs, ref = await setup(session, test_settings, 7001)

    approved = await orchestrator.handle_button(turn, Action.POST_APPROVE, ref.value())
    assert "✅ Одобрено" in approved.text and "Когда публикуем в «Зерно · Казань»" in approved.text
    assert approved.close_source
    options = [
        b.value.rsplit(".", 1)[-1] for row in approved.buttons for b in row if b.action is Action.POST_AT
    ]
    assert "m10" in options and "plan" in options

    scheduled = await orchestrator.handle_button(turn, Action.POST_AT, f"{ref.value()}.m10")
    assert "выйдет" in scheduled.text
    variant = await session.get(PublicationVariant, ref.variant_id)
    assert variant is not None and variant.status == S.SCHEDULED and variant.scheduled_at is not None
    local = variant.scheduled_at.astimezone(MSK)
    assert (local.date(), local.hour) == (datetime.now(MSK).date() + timedelta(days=1), 10)
    assert jobs.publish_jobs == []  # время не наступило — публикует диспетчер

    now = await orchestrator.handle_button(turn, Action.POST_NOW, ref.value())
    assert "Публикую «Осенний раф» в «Зерно · Казань»" in now.text
    assert jobs.publish_jobs == [PublishJob(ref.variant_id, 1)]


async def test_approval_without_channel_explains_how_to_connect(
    session: AsyncSession, test_settings: Settings
) -> None:
    turn, orchestrator, _, ref = await setup(session, test_settings, 7002, channel=False)
    approved = await orchestrator.handle_button(turn, Action.POST_APPROVE, ref.value())
    assert "подключите канал" in approved.text and "Публикация сообщений" in approved.text
    refused = await orchestrator.handle_button(turn, Action.POST_NOW, ref.value())
    assert "Публиковать пока некуда" in refused.text
    assert await status_of(session, ref) == S.APPROVED


async def test_free_text_publish_does_not_approve(session: AsyncSession, test_settings: Settings) -> None:
    turn, orchestrator, jobs, ref = await setup(
        session, test_settings, 7003, intent("schedule_post", when="завтра в 12:00")
    )
    card = await orchestrator.handle_button(turn, Action.POST_OPEN, ref.variant_id.hex)
    await orchestrator.presented(turn, card, [501])

    reply = await orchestrator.handle_text(turn, "опубликуй завтра в 12:00")
    assert "ещё не одобрен" in reply.text and reply.followups[0].post_ref == ref
    assert await status_of(session, ref) == S.PENDING_APPROVAL and jobs.publish_jobs == []


async def test_schedule_in_words_after_approval(session: AsyncSession, test_settings: Settings) -> None:
    turn, orchestrator, _, ref = await setup(
        session, test_settings, 7004, intent("schedule_post", when="завтра в 12:00")
    )
    await orchestrator.presented(
        turn, await orchestrator.handle_button(turn, Action.POST_OPEN, ref.variant_id.hex), [502]
    )
    await orchestrator.handle_button(turn, Action.POST_APPROVE, ref.value())

    reply = await orchestrator.handle_text(turn, "Опубликуй согласованный пост завтра в 12:00")
    assert "выйдет" in reply.text
    variant = await session.get(PublicationVariant, ref.variant_id)
    assert variant is not None and variant.scheduled_at is not None
    assert variant.scheduled_at.astimezone(MSK).hour == 12


async def test_custom_time_question_and_unclear_answer(
    session: AsyncSession, test_settings: Settings
) -> None:
    turn, orchestrator, _, ref = await setup(session, test_settings, 7005)
    await orchestrator.handle_button(turn, Action.POST_APPROVE, ref.value())
    ask = await orchestrator.handle_button(turn, Action.POST_AT, f"{ref.value()}.custom")
    assert ask.expect is Expect.POST_TIME and ask.expect_data == {"ref": ref.value()}

    unclear = await orchestrator.handle_post_input(turn, Expect.POST_TIME, ask.expect_data, "когда-нибудь")
    assert "Не понял время" in unclear.text and unclear.expect is Expect.POST_TIME
    done = await orchestrator.handle_post_input(turn, Expect.POST_TIME, ask.expect_data, "через 3 часа")
    assert "выйдет" in done.text and await status_of(session, ref) == S.SCHEDULED


async def test_edit_button_then_instructions_start_revision(
    session: AsyncSession, test_settings: Settings
) -> None:
    turn, orchestrator, jobs, ref = await setup(session, test_settings, 7006)
    await orchestrator.handle_button(turn, Action.POST_APPROVE, ref.value())

    ask = await orchestrator.handle_button(turn, Action.POST_EDIT, ref.value())
    assert ask.expect is Expect.POST_EDIT
    reply = await orchestrator.handle_post_input(turn, Expect.POST_EDIT, ask.expect_data, "короче и с юмором")
    assert "Переписываю «Осенний раф»: короче и с юмором" in reply.text
    assert await status_of(session, ref) == S.NEEDS_REVISION  # одобрение отозвано
    job = jobs.content_jobs[-1]
    assert (job.kind, job.variant_id, job.instructions, job.chat_id) == (
        ContentJobKind.REVISE,
        ref.variant_id,
        "короче и с юмором",
        777,
    )


async def test_owner_can_replace_text_entirely(session: AsyncSession, test_settings: Settings) -> None:
    turn, orchestrator, jobs, ref = await setup(session, test_settings, 7007)
    data = {"ref": ref.value()}
    reply = await orchestrator.handle_post_input(
        turn,
        Expect.POST_EDIT,
        data,
        "Текст: Раф за 5 < 10 минут #кофе",
        html="Текст: Раф за 5 &lt; 10 минут #кофе",
    )
    assert reply.post_ref == PostRef(ref.variant_id, 2) and jobs.content_jobs == []
    version = await session.scalar(
        select(PublicationVersion).where(
            PublicationVersion.variant_id == ref.variant_id, PublicationVersion.version_no == 2
        )
    )
    assert version is not None and version.body == "Раф за 5 &lt; 10 минут #кофе"
    assert version.created_by_type == ActorType.USER and version.hashtags == ["#кофе"]
    assert await status_of(session, ref) == S.PENDING_APPROVAL


async def test_reply_to_post_is_an_edit_but_thanks_is_not(
    session: AsyncSession, test_settings: Settings
) -> None:
    turn, orchestrator, jobs, ref = await setup(
        session,
        test_settings,
        7008,
        intent("edit_post", instructions="добавь про корицу"),
        intent("smalltalk"),
    )
    card = await orchestrator.handle_button(turn, Action.POST_OPEN, ref.variant_id.hex)
    await orchestrator.presented(turn, card, [601, 602])

    edit = await orchestrator.handle_text(turn, "добавь про корицу", reply_to_message_id=602)
    assert "Переписываю" in edit.text and jobs.content_jobs[-1].variant_id == ref.variant_id

    thanks = await orchestrator.handle_text(turn, "спасибо!", reply_to_message_id=601)
    assert "Переписываю" not in thanks.text and len(jobs.content_jobs) == 1


async def test_old_version_buttons_do_not_act(session: AsyncSession, test_settings: Settings) -> None:
    turn, orchestrator, _, ref = await setup(session, test_settings, 7009)
    await orchestrator.handle_post_input(turn, Expect.POST_EDIT, {"ref": ref.value()}, "Текст: Новый текст")
    stale = await orchestrator.handle_button(turn, Action.POST_APPROVE, ref.value())  # кнопка под v1
    assert "старая версия" in stale.text
    assert await status_of(session, ref) == S.PENDING_APPROVAL
    broken = await orchestrator.handle_button(turn, Action.POST_APPROVE, "испорчено")
    assert "Кнопка устарела" in broken.text


async def test_image_and_redo_buttons_enqueue_jobs(session: AsyncSession, test_settings: Settings) -> None:
    turn, orchestrator, jobs, ref = await setup(session, test_settings, 7010)
    image = await orchestrator.handle_button(turn, Action.POST_IMAGE, ref.value())
    assert "Рисую картинку" in image.text and image.close_source
    redo = await orchestrator.handle_button(turn, Action.POST_REDO, ref.value())
    assert "другой вариант" in redo.text
    assert [(j.kind, j.variant_id) for j in jobs.content_jobs] == [
        (ContentJobKind.IMAGE, ref.variant_id),
        (ContentJobKind.REVISE, ref.variant_id),
    ]
    keep = await orchestrator.handle_button(turn, Action.POST_KEEP, ref.value())
    assert keep.post_ref == ref and await status_of(session, ref) == S.PENDING_APPROVAL


async def test_schedule_overview_and_pending_list(session: AsyncSession, test_settings: Settings) -> None:
    turn, orchestrator, _, ref = await setup(session, test_settings, 7011)
    overview = await orchestrator.handle_command(turn, Intent.SHOW_SCHEDULE, "/schedule")
    assert "Канал: «Зерно · Казань» ✅" in overview.text
    groups = [b.value for row in overview.buttons for b in row if b.action is Action.POST_LIST]
    assert groups == ["pending"]

    pending = await orchestrator.handle_button(turn, Action.POST_LIST, "pending")
    assert "Ждут одобрения: 1" in pending.text and pending.followups[0].post_ref == ref

    await orchestrator.handle_button(turn, Action.POST_APPROVE, ref.value())
    await orchestrator.handle_button(turn, Action.POST_AT, f"{ref.value()}.m19")
    overview = await orchestrator.handle_command(turn, Intent.SHOW_SCHEDULE, "/schedule")
    assert "Запланировано (1)" in overview.text and "«Осенний раф»" in overview.text

    cancel_choice = await orchestrator.handle_command(turn, Intent.CANCEL_POST, "отмени")
    assert cancel_choice.buttons[0][0].action is Action.POST_UNSCHEDULE
    unscheduled = await orchestrator.handle_button(turn, Action.POST_UNSCHEDULE, ref.value())
    assert "Снял «Осенний раф» с расписания" in unscheduled.text
    assert await status_of(session, ref) == S.APPROVED


async def test_reject_button(session: AsyncSession, test_settings: Settings) -> None:
    turn, orchestrator, _, ref = await setup(session, test_settings, 7012)
    rejected = await orchestrator.handle_button(turn, Action.POST_REJECT, ref.value())
    assert "Отклонил" in rejected.text and rejected.buttons[0][0].action is Action.POST_REDO
    assert await status_of(session, ref) == S.REJECTED


async def test_channel_is_connected_only_with_rights(session: AsyncSession, test_settings: Settings) -> None:
    turn, orchestrator, _, _ = await setup(session, test_settings, 7013, channel=False)
    owner_id = turn.user.telegram_user_id
    from aiogram.types import ChatMemberOwner
    from aiogram.types import User as TgUser

    owner = ChatMemberOwner(user=TgUser(id=owner_id, is_bot=False, first_name="x"), is_anonymous=False)

    no_rights = await connect_channel(
        ChannelBot({owner_id: owner, 999: admin(999, can_post=False)}),  # type: ignore[arg-type]
        session,
        turn,
        "@zerno_kzn",
    )
    assert "Публикация сообщений" in no_rights.text
    ok = await connect_channel(
        ChannelBot({owner_id: owner, 999: admin(999, can_post=True)}),  # type: ignore[arg-type]
        session,
        turn,
        "@zerno_kzn",
    )
    assert "подключён к проекту «Зерно»" in ok.text
    assert turn.project is not None
    accounts = list(
        await session.scalars(select(SocialAccount).where(SocialAccount.project_id == turn.project.id))
    )
    assert len(accounts) == 1 and accounts[0].can_post and accounts[0].status == SocialAccountStatus.ACTIVE

    settings_reply = await orchestrator.handle_command(turn, Intent.SETTINGS, "/settings")
    assert "Telegram-канал: «Зерно · Казань» ✅" in settings_reply.text


async def test_presented_preview_moves_draft_to_pending(
    session: AsyncSession, test_settings: Settings
) -> None:
    turn, orchestrator, _, ref = await setup(session, test_settings, 7014)
    assert await status_of(session, ref) == S.DRAFT
    await orchestrator.presented(turn, Reply(text="x", post_ref=ref), [700])
    assert await status_of(session, ref) == S.PENDING_APPROVAL
