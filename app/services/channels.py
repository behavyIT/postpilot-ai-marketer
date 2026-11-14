"""Подключённые площадки проекта (пока — Telegram-каналы)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Project, Publication, PublicationVariant, SocialAccount, User
from app.domain.enums import ActorType, Platform, PublicationStatus, SocialAccountStatus
from app.integrations.social.base import ChannelCheck
from app.services.audit import AuditService


class ChannelService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def connect(
        self, user: User, project: Project, check: ChannelCheck, *, platform: Platform = Platform.TELEGRAM
    ) -> tuple[SocialAccount, bool]:
        """Привязать канал к проекту. У проекта один канал на площадку: прежний отключается,
        а запланированные посты переезжают в новый."""
        external_id = str(check.chat_id)
        account = await self._session.scalar(
            select(SocialAccount).where(
                SocialAccount.project_id == project.id,
                SocialAccount.platform == platform,
                SocialAccount.external_id == external_id,
            )
        )
        created = account is None
        if account is None:
            account = SocialAccount(project_id=project.id, platform=platform, external_id=external_id)
            self._session.add(account)
        account.title = check.title[:255]
        account.username = check.username
        account.can_post = check.can_post
        account.capabilities = check.capabilities
        account.status = SocialAccountStatus.ACTIVE if check.can_post else SocialAccountStatus.NO_RIGHTS
        account.last_checked_at = datetime.now(UTC)
        account.last_error = "; ".join(check.problems) or None
        account.connected_by_user_id = user.id
        await self._session.flush()

        if check.can_post:
            await self._session.execute(
                update(SocialAccount)
                .where(
                    SocialAccount.project_id == project.id,
                    SocialAccount.platform == platform,
                    SocialAccount.id != account.id,
                    SocialAccount.status == SocialAccountStatus.ACTIVE,
                )
                .values(status=SocialAccountStatus.DISCONNECTED)
            )
            scheduled = (
                select(PublicationVariant.id)
                .join(Publication, Publication.id == PublicationVariant.publication_id)
                .where(
                    Publication.project_id == project.id,
                    PublicationVariant.platform == platform,
                    PublicationVariant.status == PublicationStatus.SCHEDULED,
                )
            )
            await self._session.execute(
                update(PublicationVariant)
                .where(PublicationVariant.id.in_(scheduled))
                .values(social_account_id=account.id)
            )
        await AuditService(self._session).record(
            action="channel.connected" if check.can_post else "channel.no_rights",
            actor_type=ActorType.USER,
            actor_id=user.id,
            project_id=project.id,
            entity_type="social_account",
            entity_id=account.id,
            data={"title": account.title, "problems": check.problems},
        )
        return account, created

    async def mark_by_chat(
        self, external_id: str, *, status: SocialAccountStatus, reason: str
    ) -> list[SocialAccount]:
        """Бота удалили из канала или лишили прав — во всех проектах, где канал подключён."""
        accounts = list(
            await self._session.scalars(
                select(SocialAccount).where(
                    SocialAccount.external_id == external_id,
                    SocialAccount.status != SocialAccountStatus.DISCONNECTED,
                )
            )
        )
        for account in accounts:
            account.status = status
            account.can_post = False
            account.last_error = reason
            account.last_checked_at = datetime.now(UTC)
        await self._session.flush()
        return accounts

    async def current(self, project_id: UUID, platform: Platform = Platform.TELEGRAM) -> SocialAccount | None:
        """Последний подключённый канал проекта, даже если права пропали (для показа в настройках)."""
        return await self._session.scalar(
            select(SocialAccount)
            .where(
                SocialAccount.project_id == project_id,
                SocialAccount.platform == platform,
                SocialAccount.status != SocialAccountStatus.DISCONNECTED,
            )
            .order_by(SocialAccount.updated_at.desc())
            .limit(1)
        )
