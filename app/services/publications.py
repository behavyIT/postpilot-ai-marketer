"""Согласование постов: одобрение, отклонение, правки, расписание.

Все изменения статуса идут через transition(): проверка по таблице конечного автомата
и запись в журнал publication_events. Строка варианта блокируется (SELECT … FOR UPDATE),
поэтому решение владельца не может «разминуться» с публикатором.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainValidationError, NotFoundError
from app.db.models import (
    Approval,
    PostMessage,
    Project,
    Publication,
    PublicationEvent,
    PublicationVariant,
    PublicationVersion,
    SocialAccount,
    StoredFile,
    User,
)
from app.domain.enums import (
    ActorType,
    ApprovalDecision,
    Platform,
    PublicationStatus,
    SocialAccountStatus,
)
from app.domain.platforms import SPECS, validate_for_platform
from app.domain.publications.hashing import content_hash
from app.domain.publications.refs import PostRef
from app.domain.publications.state_machine import EDITABLE, LABELS, ensure_transition

S = PublicationStatus
MAX_SCHEDULE_AHEAD = timedelta(days=90)
PAST_TOLERANCE = timedelta(minutes=1)


class StaleVersionError(DomainValidationError):
    """Кнопка относится к старой версии поста."""


class ChannelMissingError(DomainValidationError):
    """Публиковать некуда: канал не подключён или у бота нет прав."""


@dataclass(slots=True)
class PostView:
    publication: Publication
    variant: PublicationVariant
    version: PublicationVersion
    media: StoredFile | None
    account: SocialAccount | None


class PublicationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- Чтение -------------------------------------------------------------------------------

    async def get_view(self, project_id: UUID, variant_id: UUID, *, lock: bool = False) -> PostView:
        stmt = (
            select(PublicationVariant, Publication)
            .join(Publication, Publication.id == PublicationVariant.publication_id)
            .where(PublicationVariant.id == variant_id, Publication.project_id == project_id)
        )
        if lock:
            stmt = stmt.with_for_update(of=PublicationVariant)
        row = (await self._session.execute(stmt)).first()
        if row is None:
            raise NotFoundError("Пост не найден")
        variant, publication = row
        return await self._view(publication, variant)

    async def _view(self, publication: Publication, variant: PublicationVariant) -> PostView:
        version = await self._session.scalar(
            select(PublicationVersion).where(
                PublicationVersion.variant_id == variant.id,
                PublicationVersion.version_no == variant.current_version_no,
            )
        )
        if version is None:
            raise NotFoundError("У поста ещё нет текста")
        media = await self._session.get(StoredFile, version.media_file_id) if version.media_file_id else None
        account = (
            await self._session.get(SocialAccount, variant.social_account_id)
            if variant.social_account_id
            else None
        )
        return PostView(
            publication=publication, variant=variant, version=version, media=media, account=account
        )

    async def list_views(
        self,
        project_id: UUID,
        statuses: set[PublicationStatus],
        *,
        limit: int = 10,
        newest_first: bool = False,
    ) -> list[PostView]:
        order = (
            [PublicationVariant.updated_at.desc()]
            if newest_first
            else [PublicationVariant.scheduled_at, Publication.planned_at, PublicationVariant.created_at]
        )
        rows = (
            await self._session.execute(
                select(PublicationVariant, Publication)
                .join(Publication, Publication.id == PublicationVariant.publication_id)
                .where(
                    Publication.project_id == project_id,
                    PublicationVariant.status.in_([s.value for s in statuses]),
                    PublicationVariant.current_version_no > 0,
                )
                .order_by(*order)
                .limit(limit)
            )
        ).all()
        return [await self._view(publication, variant) for variant, publication in rows]

    async def count(self, project_id: UUID, statuses: set[PublicationStatus]) -> int:
        rows = await self._session.scalars(
            select(PublicationVariant.id)
            .join(Publication, Publication.id == PublicationVariant.publication_id)
            .where(
                Publication.project_id == project_id,
                PublicationVariant.status.in_([s.value for s in statuses]),
            )
        )
        return len(list(rows))

    # --- Сообщения с превью ---------------------------------------------------------------------

    async def presented(self, ref: PostRef, *, chat_id: int, message_ids: list[int]) -> None:
        """Превью показано владельцу: запоминаем сообщения и переводим черновик на согласование."""
        for message_id in message_ids:
            exists = await self._session.scalar(
                select(PostMessage.id).where(
                    PostMessage.chat_id == chat_id, PostMessage.message_id == message_id
                )
            )
            if exists is None:
                self._session.add(
                    PostMessage(
                        chat_id=chat_id,
                        message_id=message_id,
                        variant_id=ref.variant_id,
                        version_no=ref.version_no,
                    )
                )
        variant = await self._session.get(PublicationVariant, ref.variant_id, with_for_update=True)
        if variant is not None and variant.status == S.DRAFT:
            await self.transition(
                variant, S.PENDING_APPROVAL, actor=ActorType.SYSTEM, reason="показан владельцу"
            )
        await self._session.flush()

    async def ref_for_message(self, chat_id: int, message_id: int) -> PostRef | None:
        row = (
            await self._session.execute(
                select(PostMessage.variant_id, PostMessage.version_no).where(
                    PostMessage.chat_id == chat_id, PostMessage.message_id == message_id
                )
            )
        ).first()
        return PostRef(row[0], row[1]) if row else None

    async def focused(self, chat_id: int, project_id: UUID) -> UUID | None:
        """Пост, который владелец видел последним в этом чате, — к нему относятся «сократи», «опубликуй»."""
        return await self._session.scalar(
            select(PostMessage.variant_id)
            .join(PublicationVariant, PublicationVariant.id == PostMessage.variant_id)
            .join(Publication, Publication.id == PublicationVariant.publication_id)
            .where(PostMessage.chat_id == chat_id, Publication.project_id == project_id)
            .order_by(PostMessage.created_at.desc(), PostMessage.id.desc())
            .limit(1)
        )

    # --- Переходы -----------------------------------------------------------------------------

    async def transition(
        self,
        variant: PublicationVariant,
        target: PublicationStatus,
        *,
        actor: ActorType,
        user_id: UUID | None = None,
        reason: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        current = S(variant.status)
        ensure_transition(current, target)
        self._session.add(
            PublicationEvent(
                variant_id=variant.id,
                from_status=current,
                to_status=target,
                actor_type=actor,
                actor_user_id=user_id,
                reason=reason,
                meta=meta or {},
            )
        )
        variant.status = target
        if current is S.SCHEDULED and target in {S.APPROVED, S.NEEDS_REVISION, S.CANCELLED}:
            variant.scheduled_at = None  # задача расписания аннулирована

    async def approve(self, user: User, project_id: UUID, ref: PostRef) -> PostView:
        view = await self._locked_current(project_id, ref)
        variant, version = view.variant, view.version
        if variant.status in {S.APPROVED, S.SCHEDULED} and variant.approved_version_no == version.version_no:
            return view  # повторное нажатие
        if variant.status == S.DRAFT:
            await self.transition(
                variant, S.PENDING_APPROVAL, actor=ActorType.SYSTEM, reason="показан владельцу"
            )
        if variant.status != S.PENDING_APPROVAL:
            raise DomainValidationError(f"Пост сейчас {LABELS[S(variant.status)]} — одобрять нечего")
        blocking = blocking_problems(view)
        if blocking:
            raise DomainValidationError("Такой пост площадка не примет: " + "; ".join(blocking))
        await self.transition(variant, S.APPROVED, actor=ActorType.USER, user_id=user.id, reason="одобрено")
        variant.approved_version_no = version.version_no
        self._session.add(
            Approval(
                variant_id=variant.id,
                version_no=version.version_no,
                decision=ApprovalDecision.APPROVED,
                content_hash=version.content_hash,
                decided_by_user_id=user.id,
            )
        )
        await self._session.flush()
        return view

    async def reject(self, user: User, project_id: UUID, ref: PostRef) -> PostView:
        view = await self._locked_current(project_id, ref)
        variant = view.variant
        if variant.status == S.REJECTED:
            return view
        if variant.status == S.DRAFT:
            await self.transition(
                variant, S.PENDING_APPROVAL, actor=ActorType.SYSTEM, reason="показан владельцу"
            )
        await self.transition(variant, S.REJECTED, actor=ActorType.USER, user_id=user.id, reason="отклонено")
        self._session.add(
            Approval(
                variant_id=variant.id,
                version_no=view.version.version_no,
                decision=ApprovalDecision.REJECTED,
                content_hash=view.version.content_hash,
                decided_by_user_id=user.id,
            )
        )
        await self._session.flush()
        return view

    async def start_revision(
        self, user: User, project_id: UUID, variant_id: UUID, *, request: str | None
    ) -> PostView:
        """Владелец просит изменить пост: прежнее одобрение больше не действует."""
        view = await self.get_view(project_id, variant_id, lock=True)
        variant = view.variant
        status = S(variant.status)
        if status not in EDITABLE:
            raise DomainValidationError(f"Пост {LABELS[status]} — изменить его уже нельзя")
        if status == S.DRAFT:
            await self.transition(
                variant, S.PENDING_APPROVAL, actor=ActorType.SYSTEM, reason="показан владельцу"
            )
        if variant.status != S.NEEDS_REVISION:
            await self.transition(
                variant,
                S.NEEDS_REVISION,
                actor=ActorType.USER,
                user_id=user.id,
                reason="запрошена правка",
                meta={"request": (request or "")[:500]},
            )
            self._session.add(
                Approval(
                    variant_id=variant.id,
                    version_no=view.version.version_no,
                    decision=ApprovalDecision.REVISION_REQUESTED,
                    content_hash=view.version.content_hash,
                    decided_by_user_id=user.id,
                    comment=request,
                )
            )
        variant.approved_version_no = None
        await self._session.flush()
        return view

    async def add_version(
        self,
        variant: PublicationVariant,
        *,
        body: str,
        hashtags: list[str],
        actor: ActorType,
        user_id: UUID | None = None,
        change_request: str | None = None,
        used_fact_ids: list[str] | None = None,
        warnings: list[str] | None = None,
        media: StoredFile | None = None,
        image_prompt: str | None = None,
    ) -> PublicationVersion:
        """Новая неизменяемая версия; пост снова ждёт одобрения."""
        if variant.status not in {S.DRAFT, S.NEEDS_REVISION}:
            raise DomainValidationError(f"Пост {LABELS[S(variant.status)]} — новую версию создать нельзя")
        version = PublicationVersion(
            variant_id=variant.id,
            version_no=variant.current_version_no + 1,
            body=body,
            hashtags=hashtags,
            content_hash=content_hash(body, media.sha256 if media else None),
            created_by_type=actor,
            change_request=change_request,
            used_fact_ids=used_fact_ids or [],
            warnings=warnings or [],
            media_file_id=media.id if media else None,
            image_prompt=image_prompt,
        )
        self._session.add(version)
        variant.current_version_no = version.version_no
        variant.approved_version_no = None
        if variant.status == S.NEEDS_REVISION:
            await self.transition(
                variant,
                S.PENDING_APPROVAL,
                actor=actor,
                user_id=user_id,
                reason=f"создана версия {version.version_no}",
            )
        await self._session.flush()
        return version

    async def schedule(
        self, user: User, project: Project, ref: PostRef, at: datetime, *, now: datetime | None = None
    ) -> PostView:
        now = now or datetime.now(UTC)
        if at < now - PAST_TOLERANCE:
            raise DomainValidationError("Это время уже прошло — выберите другое")
        if at > now + MAX_SCHEDULE_AHEAD:
            raise DomainValidationError("Запланировать можно не дальше чем на 90 дней вперёд")
        view = await self._locked_current(project.id, ref)
        variant = view.variant
        if variant.status not in {S.APPROVED, S.SCHEDULED}:
            if variant.status in {S.DRAFT, S.PENDING_APPROVAL}:
                raise DomainValidationError("Сначала одобрите пост — без одобрения публиковать не буду")
            raise DomainValidationError(f"Пост {LABELS[S(variant.status)]} — запланировать нельзя")
        if variant.approved_version_no != variant.current_version_no:
            raise DomainValidationError("Эта версия поста не одобрена — одобрите её заново")
        account = await active_account(self._session, project.id, Platform(variant.platform))
        if account is None:
            raise ChannelMissingError("Не подключён канал для публикации")
        if variant.status == S.SCHEDULED:
            await self.transition(
                variant, S.APPROVED, actor=ActorType.USER, user_id=user.id, reason="перенос"
            )
        variant.social_account_id = account.id
        variant.scheduled_at = max(at, now)
        variant.failure_reason = None
        await self.transition(
            variant,
            S.SCHEDULED,
            actor=ActorType.USER,
            user_id=user.id,
            reason="запланировано",
            meta={"at": variant.scheduled_at.isoformat()},
        )
        await self._session.flush()
        view.account = account
        return view

    async def unschedule(self, user: User, project_id: UUID, variant_id: UUID) -> PostView:
        view = await self.get_view(project_id, variant_id, lock=True)
        if view.variant.status == S.APPROVED:
            return view
        if view.variant.status != S.SCHEDULED:
            raise DomainValidationError(
                f"Пост {LABELS[S(view.variant.status)]} — снимать с расписания нечего"
            )
        await self.transition(
            view.variant, S.APPROVED, actor=ActorType.USER, user_id=user.id, reason="снято с расписания"
        )
        await self._session.flush()
        return view

    async def retry(
        self, user: User, project: Project, variant_id: UUID, *, now: datetime | None = None
    ) -> PostView:
        """«Повторить» после неудачи: снова в расписание на сейчас, со всеми проверками."""
        now = now or datetime.now(UTC)
        view = await self.get_view(project.id, variant_id, lock=True)
        variant = view.variant
        if variant.status != S.FAILED:
            raise DomainValidationError(f"Пост {LABELS[S(variant.status)]} — повторять нечего")
        if variant.approved_version_no != variant.current_version_no:
            raise DomainValidationError("Эта версия поста не одобрена — одобрите её заново")
        account = await active_account(self._session, project.id, Platform(variant.platform))
        if account is None:
            raise ChannelMissingError("Не подключён канал для публикации")
        variant.social_account_id = account.id
        variant.scheduled_at = now
        variant.failure_reason = None
        await self.transition(variant, S.SCHEDULED, actor=ActorType.USER, user_id=user.id, reason="повтор")
        await self._session.flush()
        view.account = account
        return view

    async def confirm_published(self, user: User, project_id: UUID, variant_id: UUID) -> PostView:
        """Исход публикации был неизвестен, владелец проверил канал: пост вышел."""
        view = await self.get_view(project_id, variant_id, lock=True)
        variant = view.variant
        if variant.status == S.PUBLISHED:
            return view
        if variant.status != S.FAILED:
            raise DomainValidationError(f"Пост {LABELS[S(variant.status)]}")
        await self.transition(
            variant,
            S.PUBLISHED,
            actor=ActorType.USER,
            user_id=user.id,
            reason="владелец подтвердил выход поста",
        )
        variant.published_at = datetime.now(UTC)
        variant.failure_reason = None
        await self._session.flush()
        return view

    async def cancel(self, user: User, project_id: UUID, variant_id: UUID) -> PostView:
        view = await self.get_view(project_id, variant_id, lock=True)
        if view.variant.status == S.CANCELLED:
            return view
        await self.transition(
            view.variant, S.CANCELLED, actor=ActorType.USER, user_id=user.id, reason="отменено"
        )
        await self._session.flush()
        return view

    async def _locked_current(self, project_id: UUID, ref: PostRef) -> PostView:
        view = await self.get_view(project_id, ref.variant_id, lock=True)
        if view.variant.current_version_no != ref.version_no:
            raise StaleVersionError(
                f"Это старая версия поста (v{ref.version_no}). "
                f"Актуальная — v{view.variant.current_version_no}: откройте её через /schedule"
            )
        return view


async def active_account(session: AsyncSession, project_id: UUID, platform: Platform) -> SocialAccount | None:
    return await session.scalar(
        select(SocialAccount)
        .where(
            SocialAccount.project_id == project_id,
            SocialAccount.platform == platform,
            SocialAccount.status == SocialAccountStatus.ACTIVE,
            SocialAccount.can_post.is_(True),
        )
        .order_by(SocialAccount.updated_at.desc())
        .limit(1)
    )


def blocking_problems(view: PostView) -> list[str]:
    """Нарушения, с которыми площадка пост не примет (лимиты длины, разметка)."""
    spec = SPECS[Platform(view.variant.platform)]
    return validate_for_platform(view.version.body, spec, with_image=view.media is not None)
