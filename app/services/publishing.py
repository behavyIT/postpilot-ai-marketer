"""Публикация одобренных постов по расписанию (выполняется в воркере).

Порядок (§4.9 технического документа):
1. Транзакция с блокировкой строки варианта: проверки — статус SCHEDULED и время наступило,
   одобрена именно текущая версия и её хеш совпадает с одобренным, проект не на паузе,
   канал подключён и у бота есть права, успешной попытки ещё не было.
   Статус → PUBLISHING, запись попытки → COMMIT.
2. Только после этого — вызов API площадки.
3. Итог фиксируется в новой транзакции.

Ошибка «до отправки» (нет соединения, 429) повторяется с паузами. Если ответа нет,
пост мог выйти: автоматически не повторяем, а спрашиваем владельца.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.models import (
    Approval,
    Project,
    ProjectMember,
    Publication,
    PublicationEvent,
    PublicationVariant,
    PublicationVersion,
    PublishAttempt,
    SocialAccount,
    StoredFile,
    User,
)
from app.domain.enums import (
    ActorType,
    ApprovalDecision,
    Platform,
    ProjectStatus,
    PublicationStatus,
    PublishAttemptStatus,
    SocialAccountStatus,
)
from app.domain.publications.hashing import content_hash
from app.integrations.social.base import PostContent, Publisher, PublishError, PublishErrorKind
from app.integrations.storage.base import FileStorage
from app.services.publications import PublicationService

log = get_logger(__name__)
S = PublicationStatus
DUE_TOLERANCE = timedelta(seconds=5)
STALE_AFTER = timedelta(minutes=10)


class OutcomeKind(StrEnum):
    PUBLISHED = "published"
    SKIPPED = "skipped"  # публиковать не нужно или рано — молча выходим
    FAILED = "failed"  # не опубликовано; владелец может повторить
    UNKNOWN = "unknown"  # исход неизвестен; владелец проверяет канал
    BLOCKED = "blocked"  # проверка не пройдена; пост вернулся на доработку


@dataclass(slots=True)
class PublishOutcome:
    kind: OutcomeKind
    variant_id: UUID
    reason: str = ""
    project_id: UUID | None = None
    title: str = ""
    channel_title: str = ""
    url: str | None = None
    notify: list[int] = field(default_factory=list)  # Telegram id тех, кому сообщить


@dataclass(slots=True)
class _Prepared:
    attempt_id: UUID
    attempt_no: int
    idempotency_key: str
    target: str
    platform: Platform
    content: PostContent
    outcome: PublishOutcome


class PublishingService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        publishers: Mapping[Platform, Publisher],
        storage: FileStorage,
        max_attempts: int = 5,
        base_delay_s: float = 2.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sf = session_factory
        self._publishers = publishers
        self._storage = storage
        self._max_attempts = max_attempts
        self._base_delay_s = base_delay_s
        self._sleep = sleep

    async def publish(self, variant_id: UUID, *, now: datetime | None = None) -> PublishOutcome:
        async with self._sf() as session:
            prepared = await self._prepare(session, variant_id, now or datetime.now(UTC))
            await session.commit()
        if isinstance(prepared, PublishOutcome):
            return prepared

        publisher = self._publishers[prepared.platform]
        attempt_id, attempt_no = prepared.attempt_id, prepared.attempt_no
        while True:
            try:
                result = await publisher.publish(prepared.target, prepared.content)
            except PublishError as exc:
                retry = exc.kind is PublishErrorKind.RETRYABLE and attempt_no < self._max_attempts
                if not retry:
                    return await self._fail(variant_id, attempt_id, exc, prepared.outcome)
                attempt_id, attempt_no = await self._next_attempt(variant_id, attempt_id, attempt_no, exc)
                delay = exc.retry_after or self._base_delay_s * 2 ** (attempt_no - 2)
                log.warning("publish.retry", variant_id=str(variant_id), attempt=attempt_no, delay_s=delay)
                await self._sleep(min(delay, 120.0))
                continue
            except Exception as exc:  # непредвиденное: исход неизвестен
                log.exception("publish.unexpected_error", variant_id=str(variant_id))
                unknown = PublishError(
                    PublishErrorKind.UNKNOWN, "unexpected", f"Непредвиденная ошибка: {exc}"
                )
                return await self._fail(variant_id, attempt_id, unknown, prepared.outcome)
            return await self._succeed(
                variant_id, attempt_id, result.external_id, result.url, prepared.outcome
            )

    # --- Шаг 1: проверки и захват --------------------------------------------------------------

    async def _prepare(
        self, session: AsyncSession, variant_id: UUID, now: datetime
    ) -> _Prepared | PublishOutcome:
        row = (
            await session.execute(
                select(PublicationVariant, Publication, Project)
                .join(Publication, Publication.id == PublicationVariant.publication_id)
                .join(Project, Project.id == Publication.project_id)
                .where(PublicationVariant.id == variant_id)
                .with_for_update(of=PublicationVariant)
            )
        ).first()
        if row is None:
            return PublishOutcome(OutcomeKind.SKIPPED, variant_id, reason="пост не найден")
        variant, publication, project = row
        outcome = PublishOutcome(
            OutcomeKind.SKIPPED,
            variant_id,
            project_id=project.id,
            title=publication.title,
            notify=await _recipients(session, variant.id, project.id),
        )
        service = PublicationService(session)

        # 1. Статус и время — повторная доставка задачи здесь и заканчивается.
        if variant.status != S.SCHEDULED:
            outcome.reason = f"статус {variant.status}"
            return outcome
        if variant.scheduled_at is None or variant.scheduled_at > now + DUE_TOLERANCE:
            outcome.reason = "время ещё не наступило"
            return outcome
        # 2. Проект не на паузе: пост остаётся в расписании до /resume.
        if project.status != ProjectStatus.ACTIVE or project.deleted_at is not None:
            outcome.reason = "проект на паузе"
            return outcome

        # 3. Одобрена именно текущая версия, и её содержимое не менялось после одобрения.
        version = await session.scalar(
            select(PublicationVersion).where(
                PublicationVersion.variant_id == variant.id,
                PublicationVersion.version_no == variant.current_version_no,
            )
        )
        media = (
            await session.get(StoredFile, version.media_file_id)
            if version and version.media_file_id
            else None
        )
        approval = await session.scalar(
            select(Approval)
            .where(Approval.variant_id == variant.id)
            .order_by(Approval.created_at.desc(), Approval.id.desc())
            .limit(1)
        )
        problem = _approval_problem(variant, version, media, approval)
        if problem is not None:
            await service.transition(variant, S.NEEDS_REVISION, actor=ActorType.SYSTEM, reason=problem)
            variant.approved_version_no = None
            outcome.kind, outcome.reason = OutcomeKind.BLOCKED, problem
            log.warning("publish.guard_failed", variant_id=str(variant.id), problem=problem)
            return outcome
        version = _must(version)  # проверено в _approval_problem

        # 4. Успешная попытка уже была — значит, пост вышел, а статус не успел обновиться.
        succeeded = await session.scalar(
            select(PublishAttempt).where(
                PublishAttempt.variant_id == variant.id,
                PublishAttempt.status == PublishAttemptStatus.SUCCEEDED,
            )
        )
        if succeeded is not None:
            await service.transition(variant, S.PUBLISHING, actor=ActorType.SYSTEM, reason="сверка")
            await service.transition(variant, S.PUBLISHED, actor=ActorType.SYSTEM, reason="уже опубликован")
            variant.external_post_id = succeeded.external_post_id
            outcome.reason = "уже опубликован"
            return outcome

        # 5. Канал подключён и бот может публиковать.
        account = (
            await session.get(SocialAccount, variant.social_account_id) if variant.social_account_id else None
        )
        await service.transition(variant, S.PUBLISHING, actor=ActorType.SYSTEM, reason="время публикации")
        if account is None or account.status != SocialAccountStatus.ACTIVE or not account.can_post:
            reason = "канал не подключён или у бота нет права публиковать"
            await service.transition(variant, S.FAILED, actor=ActorType.SYSTEM, reason=reason)
            variant.failure_reason = reason
            outcome.kind, outcome.reason = OutcomeKind.FAILED, reason
            return outcome
        outcome.channel_title = account.title

        image: bytes | None = None
        if media is not None:
            try:
                image = await self._storage.read(media.storage_key)
            except OSError:
                reason = "файл картинки не найден в хранилище"
                await service.transition(variant, S.FAILED, actor=ActorType.SYSTEM, reason=reason)
                variant.failure_reason = reason
                outcome.kind, outcome.reason = OutcomeKind.FAILED, reason
                return outcome

        key = f"{variant.id}:{version.version_no}"
        attempt_no = (
            await session.scalar(
                select(func.coalesce(func.max(PublishAttempt.attempt_no), 0)).where(
                    PublishAttempt.idempotency_key == key
                )
            )
            or 0
        ) + 1
        attempt = PublishAttempt(
            variant_id=variant.id,
            version_no=version.version_no,
            social_account_id=account.id,
            idempotency_key=key,
            attempt_no=attempt_no,
            status=PublishAttemptStatus.STARTED,
        )
        session.add(attempt)
        await session.flush()
        return _Prepared(
            attempt_id=attempt.id,
            attempt_no=attempt_no,
            idempotency_key=key,
            target=account.external_id,
            platform=Platform(variant.platform),
            content=PostContent(
                text=version.body,
                image=image,
                image_name=(media.original_name or "image.jpg") if media else "image.jpg",
            ),
            outcome=outcome,
        )

    # --- Шаг 3: итог ---------------------------------------------------------------------------

    async def _succeed(
        self, variant_id: UUID, attempt_id: UUID, external_id: str, url: str | None, outcome: PublishOutcome
    ) -> PublishOutcome:
        now = datetime.now(UTC)
        async with self._sf() as session:
            variant = _must(await session.get(PublicationVariant, variant_id, with_for_update=True))
            attempt = _must(await session.get(PublishAttempt, attempt_id))
            attempt.status = PublishAttemptStatus.SUCCEEDED
            attempt.external_post_id = external_id
            attempt.finished_at = now
            await PublicationService(session).transition(
                variant,
                S.PUBLISHED,
                actor=ActorType.SYSTEM,
                reason="опубликовано",
                meta={"post_id": external_id},
            )
            variant.published_at = now
            variant.external_post_id = external_id
            variant.external_url = url
            await session.commit()
        log.info("publish.succeeded", variant_id=str(variant_id), post_id=external_id)
        outcome.kind, outcome.url = OutcomeKind.PUBLISHED, url
        return outcome

    async def _fail(
        self, variant_id: UUID, attempt_id: UUID, error: PublishError, outcome: PublishOutcome
    ) -> PublishOutcome:
        statuses = {
            PublishErrorKind.RETRYABLE: PublishAttemptStatus.FAILED_RETRYABLE,
            PublishErrorKind.PERMANENT: PublishAttemptStatus.FAILED_PERMANENT,
            PublishErrorKind.UNKNOWN: PublishAttemptStatus.UNKNOWN,
        }
        async with self._sf() as session:
            variant = _must(await session.get(PublicationVariant, variant_id, with_for_update=True))
            attempt = _must(await session.get(PublishAttempt, attempt_id))
            attempt.status = statuses[error.kind]
            attempt.error_code = error.code
            attempt.error = error.message[:2000]
            attempt.finished_at = datetime.now(UTC)
            await PublicationService(session).transition(
                variant,
                S.FAILED,
                actor=ActorType.SYSTEM,
                reason=error.message[:500],
                meta={"code": error.code},
            )
            variant.failure_reason = error.message[:1000]
            if error.code == "forbidden":
                account = await session.get(SocialAccount, attempt.social_account_id)
                if account is not None:
                    account.status = SocialAccountStatus.NO_RIGHTS
                    account.can_post = False
                    account.last_error = error.message
            await session.commit()
        log.warning("publish.failed", variant_id=str(variant_id), kind=error.kind, code=error.code)
        outcome.kind = OutcomeKind.UNKNOWN if error.kind is PublishErrorKind.UNKNOWN else OutcomeKind.FAILED
        outcome.reason = error.message
        return outcome

    async def _next_attempt(
        self, variant_id: UUID, attempt_id: UUID, attempt_no: int, error: PublishError
    ) -> tuple[UUID, int]:
        """Попытка точно не дошла до площадки: закрываем её и открываем следующую."""
        async with self._sf() as session:
            attempt = _must(await session.get(PublishAttempt, attempt_id))
            attempt.status = PublishAttemptStatus.FAILED_RETRYABLE
            attempt.error_code = error.code
            attempt.error = error.message[:2000]
            attempt.finished_at = datetime.now(UTC)
            following = PublishAttempt(
                variant_id=variant_id,
                version_no=attempt.version_no,
                social_account_id=attempt.social_account_id,
                idempotency_key=attempt.idempotency_key,
                attempt_no=attempt_no + 1,
                status=PublishAttemptStatus.STARTED,
            )
            session.add(following)
            await session.commit()
            return following.id, following.attempt_no

    # --- Зависшие публикации ---------------------------------------------------------------------

    async def resolve_stale(self, *, now: datetime | None = None) -> list[PublishOutcome]:
        """PUBLISHING дольше 10 минут — воркер упал посреди публикации.

        Если последняя попытка точно не дошла до площадки, пост возвращается в расписание.
        Иначе исход неизвестен: статус FAILED и вопрос владельцу.
        """
        now = now or datetime.now(UTC)
        outcomes: list[PublishOutcome] = []
        async with self._sf() as session:
            rows = (
                await session.execute(
                    select(PublicationVariant, Publication)
                    .join(Publication, Publication.id == PublicationVariant.publication_id)
                    .where(
                        PublicationVariant.status == S.PUBLISHING,
                        PublicationVariant.updated_at < now - STALE_AFTER,
                    )
                    .with_for_update(of=PublicationVariant, skip_locked=True)
                    .limit(20)
                )
            ).all()
            service = PublicationService(session)
            for variant, publication in rows:
                last = await session.scalar(
                    select(PublishAttempt)
                    .where(PublishAttempt.variant_id == variant.id)
                    .order_by(PublishAttempt.created_at.desc(), PublishAttempt.attempt_no.desc())
                    .limit(1)
                )
                outcome = PublishOutcome(
                    OutcomeKind.SKIPPED,
                    variant.id,
                    project_id=publication.project_id,
                    title=publication.title,
                    notify=await _recipients(session, variant.id, publication.project_id),
                )
                if last is not None and last.status == PublishAttemptStatus.FAILED_RETRYABLE:
                    await service.transition(
                        variant, S.SCHEDULED, actor=ActorType.SYSTEM, reason="повтор после сбоя воркера"
                    )
                    outcome.reason = "возвращён в расписание"
                else:
                    reason = "публикация прервалась — не знаю, вышел ли пост"
                    if last is not None and last.status == PublishAttemptStatus.STARTED:
                        last.status = PublishAttemptStatus.UNKNOWN
                        last.finished_at = now
                    await service.transition(variant, S.FAILED, actor=ActorType.SYSTEM, reason=reason)
                    variant.failure_reason = reason
                    outcome.kind, outcome.reason = OutcomeKind.UNKNOWN, reason
                outcomes.append(outcome)
            await session.commit()
        return outcomes


async def due_variants(session: AsyncSession, *, now: datetime, limit: int = 20) -> list[tuple[UUID, int]]:
    """Посты, время публикации которых наступило (проекты на паузе пропускаются)."""
    rows = await session.execute(
        select(PublicationVariant.id, PublicationVariant.current_version_no)
        .join(Publication, Publication.id == PublicationVariant.publication_id)
        .join(Project, Project.id == Publication.project_id)
        .where(
            PublicationVariant.status == S.SCHEDULED,
            PublicationVariant.scheduled_at <= now,
            Project.status == ProjectStatus.ACTIVE,
            Project.deleted_at.is_(None),
        )
        .order_by(PublicationVariant.scheduled_at)
        .limit(limit)
    )
    return [(row[0], row[1]) for row in rows]


def _must[T](value: T | None) -> T:
    if value is None:
        raise RuntimeError("запись публикации исчезла во время работы")
    return value


def _approval_problem(
    variant: PublicationVariant,
    version: PublicationVersion | None,
    media: StoredFile | None,
    approval: Approval | None,
) -> str | None:
    if version is None:
        return "у поста нет текста"
    if variant.approved_version_no is None or variant.approved_version_no != variant.current_version_no:
        return "текущая версия поста не одобрена"
    if (
        approval is None
        or approval.decision != ApprovalDecision.APPROVED
        or approval.version_no != version.version_no
    ):
        return "нет одобрения текущей версии"
    if approval.content_hash != version.content_hash:
        return "содержимое изменилось после одобрения"
    if content_hash(version.body, media.sha256 if media else None) != version.content_hash:
        return "текст или картинка не совпадают с одобренными"
    return None


async def _recipients(session: AsyncSession, variant_id: UUID, project_id: UUID) -> list[int]:
    """Кому сообщить об исходе: тому, кто поставил пост в расписание, иначе — владельцу проекта."""
    user_id = await session.scalar(
        select(PublicationEvent.actor_user_id)
        .where(
            PublicationEvent.variant_id == variant_id,
            PublicationEvent.to_status == S.SCHEDULED,
            PublicationEvent.actor_user_id.is_not(None),
        )
        .order_by(PublicationEvent.created_at.desc())
        .limit(1)
    )
    stmt = select(User.telegram_user_id)
    if user_id is not None:
        stmt = stmt.where(User.id == user_id)
    else:
        stmt = stmt.join(ProjectMember, ProjectMember.user_id == User.id).where(
            ProjectMember.project_id == project_id
        )
    return list(await session.scalars(stmt))
