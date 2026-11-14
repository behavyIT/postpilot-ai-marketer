"""Согласование и публикация на настоящей БД: проверки перед отправкой, повторы, защита от дублей."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.errors import DomainValidationError
from app.db.models import (
    Approval,
    Project,
    Publication,
    PublicationEvent,
    PublicationVariant,
    PublicationVersion,
    PublishAttempt,
    SocialAccount,
    User,
)
from app.db.session import create_session_factory
from app.domain.enums import (
    ActorType,
    Platform,
    ProjectStatus,
    PublicationStatus,
    PublishAttemptStatus,
    SocialAccountStatus,
)
from app.domain.publications.refs import PostRef
from app.integrations.social.base import PostContent, PublishError, PublishErrorKind, PublishResult
from app.integrations.storage.local import LocalFileStorage
from app.scheduler.dispatcher import Dispatcher
from app.services.access import AccessService, TelegramProfile
from app.services.jobs import MemoryJobQueue, PublishJob
from app.services.publications import ChannelMissingError, PublicationService, StaleVersionError
from app.services.publishing import STALE_AFTER, OutcomeKind, PublishingService, due_variants

pytestmark = pytest.mark.integration
S = PublicationStatus
PROJECT_NAME = "Тест публикаций"


@pytest.fixture
async def sf(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = create_session_factory(engine)
    yield factory
    async with factory() as session:
        await session.execute(delete(Project).where(Project.name == PROJECT_NAME))
        await session.execute(delete(User).where(User.telegram_user_id.between(8300, 8399)))
        await session.commit()


@dataclass
class Post:
    user: User
    project: Project
    variant_id: UUID
    account: SocialAccount | None

    def ref(self, version: int = 1) -> PostRef:
        return PostRef(self.variant_id, version)


@dataclass
class FakePublisher:
    results: list[PublishResult | PublishError] = field(default_factory=list)
    delay_s: float = 0.0
    calls: list[tuple[str, PostContent]] = field(default_factory=list)
    platform: Platform = Platform.TELEGRAM

    async def publish(self, target: str, content: PostContent) -> PublishResult:
        self.calls.append((target, content))
        await asyncio.sleep(self.delay_s)
        result = self.results.pop(0) if self.results else PublishResult("55", "https://t.me/zerno_kzn/55")
        if isinstance(result, PublishError):
            raise result
        return result


async def make_post(
    sf: async_sessionmaker[AsyncSession], telegram_id: int, *, channel: bool = True, body: str = "<b>Раф</b>"
) -> Post:
    async with sf() as session:
        access = AccessService(session, default_timezone="Europe/Moscow")
        user, _ = await access.register_or_update_user(TelegramProfile(telegram_user_id=telegram_id))
        project = await access.create_project(user, PROJECT_NAME)
        publication = Publication(
            project_id=project.id,
            planned_at=datetime.now(UTC) + timedelta(days=1),
            category="Полезное",
            goal="education",
            topic="Раф",
            title="Осенний раф",
            brief="",
        )
        session.add(publication)
        await session.flush()
        variant = PublicationVariant(
            publication_id=publication.id, platform=Platform.TELEGRAM, status=S.DRAFT
        )
        session.add(variant)
        await session.flush()
        await PublicationService(session).add_version(variant, body=body, hashtags=[], actor=ActorType.AI)
        account = None
        if channel:
            account = SocialAccount(
                project_id=project.id,
                platform=Platform.TELEGRAM,
                external_id="-1001234",
                title="Зерно · Казань",
                status=SocialAccountStatus.ACTIVE,
                can_post=True,
            )
            session.add(account)
        await session.commit()
        return Post(user=user, project=project, variant_id=variant.id, account=account)


async def approve_and_schedule(
    sf: async_sessionmaker[AsyncSession], post: Post, *, at: datetime | None = None
) -> None:
    async with sf() as session:
        service = PublicationService(session)
        user = await session.get(User, post.user.id)
        project = await session.get(Project, post.project.id)
        assert user is not None and project is not None
        await service.approve(user, project.id, post.ref())
        await service.schedule(user, project, post.ref(), at or datetime.now(UTC))
        await session.commit()


def publishing(
    sf: async_sessionmaker[AsyncSession], publisher: FakePublisher, tmp_path: Any
) -> PublishingService:
    async def no_sleep(_: float) -> None:
        return None

    publishers: dict[Platform, Any] = {Platform.TELEGRAM: publisher}
    return PublishingService(sf, publishers=publishers, storage=LocalFileStorage(tmp_path), sleep=no_sleep)


async def variant_row(sf: async_sessionmaker[AsyncSession], variant_id: UUID) -> PublicationVariant:
    async with sf() as session:
        variant = await session.get(PublicationVariant, variant_id)
        assert variant is not None
        return variant


async def attempts(sf: async_sessionmaker[AsyncSession], variant_id: UUID) -> list[str]:
    async with sf() as session:
        rows = await session.scalars(
            select(PublishAttempt.status)
            .where(PublishAttempt.variant_id == variant_id)
            .order_by(PublishAttempt.attempt_no)
        )
        return list(rows)


# --- Согласование ---------------------------------------------------------------------------------


async def test_approval_is_bound_to_version_and_edit_revokes_it(sf, tmp_path) -> None:
    post = await make_post(sf, 8301)
    async with sf() as session:
        service = PublicationService(session)
        user = await session.get(User, post.user.id)
        project = await session.get(Project, post.project.id)
        assert user is not None and project is not None

        with pytest.raises(DomainValidationError, match="Сначала одобрите"):
            await service.schedule(user, project, post.ref(), datetime.now(UTC) + timedelta(hours=1))

        view = await service.approve(user, project.id, post.ref())
        assert view.variant.status == S.APPROVED and view.variant.approved_version_no == 1
        approval = await session.scalar(select(Approval).where(Approval.variant_id == post.variant_id))
        assert approval is not None and approval.content_hash == view.version.content_hash
        await service.approve(user, project.id, post.ref())  # повторное нажатие ничего не ломает

        # Правка одобренного поста: прежнее одобрение больше не действует.
        view = await service.start_revision(user, project.id, post.variant_id, request="короче")
        assert view.variant.status == S.NEEDS_REVISION and view.variant.approved_version_no is None
        await service.add_version(view.variant, body="<b>Раф</b> коротко", hashtags=[], actor=ActorType.AI)
        assert view.variant.status == S.PENDING_APPROVAL and view.variant.current_version_no == 2

        with pytest.raises(StaleVersionError, match="старая версия"):
            await service.approve(user, project.id, post.ref(1))  # кнопка под старым превью
        events = list(
            await session.scalars(
                select(PublicationEvent.to_status)
                .where(PublicationEvent.variant_id == post.variant_id)
                .order_by(PublicationEvent.created_at)
            )
        )
        assert events == ["pending_approval", "approved", "needs_revision", "pending_approval"]
        await session.commit()


async def test_scheduling_needs_a_connected_channel(sf) -> None:
    post = await make_post(sf, 8302, channel=False)
    async with sf() as session:
        service = PublicationService(session)
        user = await session.get(User, post.user.id)
        project = await session.get(Project, post.project.id)
        assert user is not None and project is not None
        await service.approve(user, project.id, post.ref())
        with pytest.raises(ChannelMissingError):
            await service.schedule(user, project, post.ref(), datetime.now(UTC) + timedelta(hours=1))
        with pytest.raises(DomainValidationError, match="уже прошло"):
            await service.schedule(user, project, post.ref(), datetime.now(UTC) - timedelta(hours=1))


async def test_too_long_caption_blocks_approval(sf) -> None:
    post = await make_post(sf, 8303, body="я" * 5000)
    async with sf() as session:
        user = await session.get(User, post.user.id)
        assert user is not None
        with pytest.raises(DomainValidationError, match="площадка не примет"):
            await PublicationService(session).approve(user, post.project.id, post.ref())


# --- Публикация -----------------------------------------------------------------------------------


async def test_due_post_is_published_once(sf, tmp_path) -> None:
    post = await make_post(sf, 8310)
    await approve_and_schedule(sf, post)
    publisher = FakePublisher()
    service = publishing(sf, publisher, tmp_path)

    outcome = await service.publish(post.variant_id)
    assert outcome.kind is OutcomeKind.PUBLISHED and outcome.url == "https://t.me/zerno_kzn/55"
    assert outcome.notify == [8310] and outcome.channel_title == "Зерно · Казань"
    assert publisher.calls[0][0] == "-1001234" and publisher.calls[0][1].text == "<b>Раф</b>"
    variant = await variant_row(sf, post.variant_id)
    assert variant.status == S.PUBLISHED and variant.external_post_id == "55"

    again = await service.publish(post.variant_id)  # повторная доставка задачи
    assert again.kind is OutcomeKind.SKIPPED and len(publisher.calls) == 1
    assert await attempts(sf, post.variant_id) == ["succeeded"]


async def test_two_workers_race_publishes_once(sf, tmp_path) -> None:
    post = await make_post(sf, 8311)
    await approve_and_schedule(sf, post)
    publisher = FakePublisher(delay_s=0.2)
    service = publishing(sf, publisher, tmp_path)

    first, second = await asyncio.gather(service.publish(post.variant_id), service.publish(post.variant_id))
    assert sorted([first.kind, second.kind]) == sorted([OutcomeKind.PUBLISHED, OutcomeKind.SKIPPED])
    assert len(publisher.calls) == 1


async def test_not_due_and_paused_posts_are_not_published(sf, tmp_path) -> None:
    post = await make_post(sf, 8312)
    await approve_and_schedule(sf, post, at=datetime.now(UTC) + timedelta(hours=2))
    publisher = FakePublisher()
    service = publishing(sf, publisher, tmp_path)
    assert (await service.publish(post.variant_id)).reason == "время ещё не наступило"

    async with sf() as session:
        await session.execute(
            update(PublicationVariant)
            .where(PublicationVariant.id == post.variant_id)
            .values(scheduled_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        await session.execute(
            update(Project).where(Project.id == post.project.id).values(status=ProjectStatus.PAUSED)
        )
        await session.commit()
        assert post.variant_id not in [v for v, _ in await due_variants(session, now=datetime.now(UTC))]
    outcome = await service.publish(post.variant_id)
    assert outcome.reason == "проект на паузе" and publisher.calls == []
    assert (await variant_row(sf, post.variant_id)).status == S.SCHEDULED  # ждёт /resume


async def test_text_changed_after_approval_is_not_published(sf, tmp_path) -> None:
    post = await make_post(sf, 8313)
    await approve_and_schedule(sf, post)
    async with sf() as session:  # подмена текста в обход сервиса
        await session.execute(
            update(PublicationVersion)
            .where(PublicationVersion.variant_id == post.variant_id)
            .values(body="<b>Скидка 90%!</b>")
        )
        await session.commit()
    publisher = FakePublisher()
    outcome = await publishing(sf, publisher, tmp_path).publish(post.variant_id)
    assert outcome.kind is OutcomeKind.BLOCKED and "не совпадают" in outcome.reason
    assert publisher.calls == []
    assert (await variant_row(sf, post.variant_id)).status == S.NEEDS_REVISION


async def test_status_forced_without_approval_is_not_published(sf, tmp_path) -> None:
    post = await make_post(sf, 8314)
    async with sf() as session:  # кто-то выставил SCHEDULED в обход одобрения
        await session.execute(
            update(PublicationVariant)
            .where(PublicationVariant.id == post.variant_id)
            .values(status=S.SCHEDULED, scheduled_at=datetime.now(UTC), social_account_id=post.account.id)  # type: ignore[union-attr]
        )
        await session.commit()
    publisher = FakePublisher()
    outcome = await publishing(sf, publisher, tmp_path).publish(post.variant_id)
    assert outcome.kind is OutcomeKind.BLOCKED and publisher.calls == []


async def test_connection_errors_are_retried_then_published(sf, tmp_path) -> None:
    post = await make_post(sf, 8315)
    await approve_and_schedule(sf, post)
    retryable = PublishError(PublishErrorKind.RETRYABLE, "connect", "Нет связи с Telegram")
    publisher = FakePublisher(results=[retryable, retryable])
    outcome = await publishing(sf, publisher, tmp_path).publish(post.variant_id)
    assert outcome.kind is OutcomeKind.PUBLISHED and len(publisher.calls) == 3
    assert await attempts(sf, post.variant_id) == ["failed_retryable", "failed_retryable", "succeeded"]


async def test_unknown_outcome_is_not_retried_automatically(sf, tmp_path) -> None:
    post = await make_post(sf, 8316)
    await approve_and_schedule(sf, post)
    unknown = PublishError(PublishErrorKind.UNKNOWN, "no_response", "Telegram не ответил — пост мог выйти")
    publisher = FakePublisher(results=[unknown])
    outcome = await publishing(sf, publisher, tmp_path).publish(post.variant_id)
    assert outcome.kind is OutcomeKind.UNKNOWN and len(publisher.calls) == 1
    assert await attempts(sf, post.variant_id) == ["unknown"]

    # Владелец проверил канал и подтвердил, что пост вышел.
    async with sf() as session:
        user = await session.get(User, post.user.id)
        assert user is not None
        view = await PublicationService(session).confirm_published(user, post.project.id, post.variant_id)
        assert view.variant.status == S.PUBLISHED
        await session.commit()


async def test_lost_rights_mark_channel(sf, tmp_path) -> None:
    post = await make_post(sf, 8317)
    await approve_and_schedule(sf, post)
    forbidden = PublishError(PublishErrorKind.PERMANENT, "forbidden", "Бот потерял доступ к каналу")
    outcome = await publishing(sf, FakePublisher(results=[forbidden]), tmp_path).publish(post.variant_id)
    assert outcome.kind is OutcomeKind.FAILED
    async with sf() as session:
        account = await session.get(SocialAccount, post.account.id)  # type: ignore[union-attr]
        assert account is not None and account.status == SocialAccountStatus.NO_RIGHTS
        # «Повторить» без прав не получится: канала, куда можно публиковать, нет.
        user = await session.get(User, post.user.id)
        project = await session.get(Project, post.project.id)
        assert user is not None and project is not None
        with pytest.raises(ChannelMissingError):
            await PublicationService(session).retry(user, project, post.variant_id)


async def test_database_forbids_second_successful_attempt(sf) -> None:
    post = await make_post(sf, 8318)
    async with sf() as session:
        for attempt_no in (1, 2):
            session.add(
                PublishAttempt(
                    variant_id=post.variant_id,
                    version_no=1,
                    social_account_id=post.account.id,  # type: ignore[union-attr]
                    idempotency_key=f"{post.variant_id}:1",
                    attempt_no=attempt_no,
                    status=PublishAttemptStatus.SUCCEEDED,
                )
            )
        with pytest.raises(IntegrityError):
            await session.flush()


async def test_stale_publishing_is_resolved(sf, tmp_path) -> None:
    post = await make_post(sf, 8319)
    await approve_and_schedule(sf, post)
    async with sf() as session:  # воркер «упал» после фиксации PUBLISHING
        variant = await session.get(PublicationVariant, post.variant_id)
        assert variant is not None
        await PublicationService(session).transition(variant, S.PUBLISHING, actor=ActorType.SYSTEM)
        session.add(
            PublishAttempt(
                variant_id=post.variant_id,
                version_no=1,
                social_account_id=post.account.id,  # type: ignore[union-attr]
                idempotency_key=f"{post.variant_id}:1",
                attempt_no=1,
            )
        )
        await session.commit()
        await session.execute(
            update(PublicationVariant)
            .where(PublicationVariant.id == post.variant_id)
            .values(updated_at=datetime.now(UTC) - STALE_AFTER - timedelta(minutes=1))
        )
        await session.commit()

    stale_calls: list[int] = []

    async def on_stale() -> None:
        stale_calls.append(1)

    dispatcher = Dispatcher(sf, MemoryJobQueue(), on_stale=on_stale)
    await dispatcher.tick()
    assert stale_calls == [1]

    outcomes = await publishing(sf, FakePublisher(), tmp_path).resolve_stale()
    mine = [o for o in outcomes if o.variant_id == post.variant_id]
    assert mine and mine[0].kind is OutcomeKind.UNKNOWN
    assert (await variant_row(sf, post.variant_id)).status == S.FAILED
    assert await attempts(sf, post.variant_id) == ["unknown"]


async def test_dispatcher_enqueues_due_posts(sf) -> None:
    post = await make_post(sf, 8320)
    await approve_and_schedule(sf, post)
    later = await make_post(sf, 8321)
    await approve_and_schedule(sf, later, at=datetime.now(UTC) + timedelta(hours=3))
    queue = MemoryJobQueue()

    async def on_stale() -> None:
        return None

    await Dispatcher(sf, queue, on_stale=on_stale).tick()
    assert PublishJob(post.variant_id, 1) in queue.publish_jobs
    assert all(job.variant_id != later.variant_id for job in queue.publish_jobs)


# --- Правки и картинки в воркере -------------------------------------------------------------------


def generation(sf: async_sessionmaker[AsyncSession], provider: Any, tmp_path: Any) -> Any:
    from app.ai.llm.base import ModelProfile
    from app.ai.llm.limiter import NullLimiter
    from app.ai.llm.service import LLMService
    from app.ai.llm.usage import MemoryUsageSink
    from app.services.content_generation import ContentGenerationService

    llm = LLMService(
        provider, models={ModelProfile.SMART: "ultra"}, limiter=NullLimiter(), usage=MemoryUsageSink()
    )
    return ContentGenerationService(
        sf, llm=llm, platforms=[Platform.TELEGRAM], storage=LocalFileStorage(tmp_path / "files")
    )


async def start_revision(sf: async_sessionmaker[AsyncSession], post: Post, request: str) -> None:
    async with sf() as session:
        user = await session.get(User, post.user.id)
        assert user is not None
        await PublicationService(session).start_revision(
            user, post.project.id, post.variant_id, request=request
        )
        await session.commit()


async def test_revision_creates_new_version_for_approval(sf, tmp_path) -> None:
    from app.ai.llm.fake import FakeLLMProvider

    post = await make_post(sf, 8330)
    await approve_and_schedule(sf, post, at=datetime.now(UTC) + timedelta(hours=5))
    await start_revision(sf, post, "короче")
    provider = FakeLLMProvider({"body": "<b>Раф</b> короче", "hashtags": ["#раф"], "used_fact_ids": []})
    outcome = await generation(sf, provider, tmp_path).revise(
        post.project.id, post.user.id, post.variant_id, instructions="короче"
    )
    assert outcome.error is None and outcome.ref == PostRef(post.variant_id, 2)
    assert "Текущая редакция поста:\n<b>Раф</b>" in provider.requests[0].messages[-1].content
    variant = await variant_row(sf, post.variant_id)
    assert variant.status == S.PENDING_APPROVAL and variant.scheduled_at is None  # расписание аннулировано
    async with sf() as session:
        version = await session.scalar(
            select(PublicationVersion).where(
                PublicationVersion.variant_id == post.variant_id, PublicationVersion.version_no == 2
            )
        )
        assert version is not None and version.body == "<b>Раф</b> короче\n\n#раф"


async def test_revision_is_dropped_if_post_changed_meanwhile(sf, tmp_path) -> None:
    from app.ai.llm.fake import FakeLLMProvider

    post = await make_post(sf, 8331)
    await start_revision(sf, post, "короче")
    async with sf() as session:  # пока модель думала, владелец отменил пост
        user = await session.get(User, post.user.id)
        assert user is not None
        await PublicationService(session).cancel(user, post.project.id, post.variant_id)
        await session.commit()
    provider = FakeLLMProvider({"body": "x", "hashtags": [], "used_fact_ids": []})
    outcome = await generation(sf, provider, tmp_path).revise(
        post.project.id, post.user.id, post.variant_id, instructions="короче"
    )
    assert outcome.ref is None and provider.requests == []
    assert (await variant_row(sf, post.variant_id)).current_version_no == 1


async def test_image_becomes_part_of_new_version(sf, tmp_path) -> None:
    from app.ai.llm.fake import FAKE_JPEG, FakeLLMProvider
    from app.db.models import StoredFile
    from app.domain.publications.hashing import content_hash

    post = await make_post(sf, 8332)
    await start_revision(sf, post, "картинка")
    provider = FakeLLMProvider()
    service = generation(sf, provider, tmp_path)
    outcome = await service.draw_image(post.project.id, post.user.id, post.variant_id, request="кот на окне")
    assert outcome.error is None and outcome.ref == PostRef(post.variant_id, 2)
    assert "кот на окне" in provider.image_requests[0][-1].content
    async with sf() as session:
        version = await session.scalar(
            select(PublicationVersion).where(
                PublicationVersion.variant_id == post.variant_id, PublicationVersion.version_no == 2
            )
        )
        assert version is not None and version.media_file_id is not None and version.body == "<b>Раф</b>"
        media = await session.get(StoredFile, version.media_file_id)
        assert media is not None and media.kind == "generated_image"
        assert version.content_hash == content_hash(version.body, media.sha256)
        assert (tmp_path / "files" / media.storage_key).read_bytes() == FAKE_JPEG

    # Одобренный пост с картинкой уходит фотографией.
    await approve_and_schedule_version(sf, post, version=2)
    publisher = FakePublisher()
    publishers: dict[Platform, Any] = {Platform.TELEGRAM: publisher}
    outcome_pub = await PublishingService(
        sf, publishers=publishers, storage=LocalFileStorage(tmp_path / "files")
    ).publish(post.variant_id)
    assert outcome_pub.kind is OutcomeKind.PUBLISHED and publisher.calls[0][1].image == FAKE_JPEG


async def approve_and_schedule_version(
    sf: async_sessionmaker[AsyncSession], post: Post, *, version: int
) -> None:
    async with sf() as session:
        service = PublicationService(session)
        user = await session.get(User, post.user.id)
        project = await session.get(Project, post.project.id)
        assert user is not None and project is not None
        await service.approve(user, project.id, post.ref(version))
        await service.schedule(user, project, post.ref(version), datetime.now(UTC))
        await session.commit()


async def test_publish_task_notifies_owner_and_releases_queue_key(sf, tmp_path, redis_client) -> None:
    from types import SimpleNamespace

    from app.bot.notifier import MemoryNotifier
    from app.workers.tasks.publish import publish_queued_key, run_publish

    post = await make_post(sf, 8333)
    await approve_and_schedule(sf, post)
    deps = SimpleNamespace(
        publishing_service=publishing(sf, FakePublisher(), tmp_path),
        notifier=MemoryNotifier(),
        redis=redis_client,
    )
    await redis_client.set(publish_queued_key(post.variant_id, 1), "1")
    await run_publish(deps, post.variant_id, 1)  # type: ignore[arg-type]
    assert await redis_client.get(publish_queued_key(post.variant_id, 1)) is None
    [(chat_id, reply)] = deps.notifier.sent
    assert chat_id == 8333 and "Опубликовано в «Зерно · Казань»" in reply.text
    assert reply.buttons[0][0].url == "https://t.me/zerno_kzn/55"
