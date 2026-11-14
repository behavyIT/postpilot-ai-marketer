"""Чтение и утверждение стратегий и контент-планов (для бота, в транзакции обработчика)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainValidationError, NotFoundError
from app.db.models import (
    ContentPlan,
    MarketingStrategy,
    Publication,
    PublicationVariant,
    PublicationVersion,
    User,
)
from app.domain.enums import ActorType, DocumentStatus
from app.services.audit import AuditService


@dataclass(slots=True)
class PlanView:
    plan: ContentPlan
    items: list[Publication]
    written: set[UUID]  # публикации, у которых уже есть текст


class ContentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    # --- Стратегия ----------------------------------------------------------------------------

    async def latest_strategy(self, project_id: UUID) -> MarketingStrategy | None:
        """Утверждённая стратегия, а если её нет — последний черновик."""
        approved = await self._session.scalar(
            select(MarketingStrategy).where(
                MarketingStrategy.project_id == project_id,
                MarketingStrategy.status == DocumentStatus.APPROVED,
            )
        )
        if approved is not None:
            return approved
        return await self._session.scalar(
            select(MarketingStrategy)
            .where(
                MarketingStrategy.project_id == project_id, MarketingStrategy.status == DocumentStatus.DRAFT
            )
            .order_by(MarketingStrategy.version_no.desc())
            .limit(1)
        )

    async def approve_strategy(self, user: User, project_id: UUID, strategy_id: UUID) -> MarketingStrategy:
        strategy = await self._session.get(MarketingStrategy, strategy_id)
        if strategy is None or strategy.project_id != project_id:
            raise NotFoundError("Стратегия не найдена")
        if strategy.status == DocumentStatus.ARCHIVED:
            raise DomainValidationError("Это устаревший вариант стратегии — откройте актуальный: /strategy")
        if strategy.status == DocumentStatus.APPROVED:
            return strategy
        await self._session.execute(
            update(MarketingStrategy)
            .where(
                MarketingStrategy.project_id == project_id,
                MarketingStrategy.status == DocumentStatus.APPROVED,
            )
            .values(status=DocumentStatus.ARCHIVED)
        )
        await self._session.flush()
        strategy.status = DocumentStatus.APPROVED
        strategy.approved_at = datetime.now(UTC)
        strategy.approved_by_user_id = user.id
        await self._session.flush()
        await self._audit.record(
            action="strategy.approved",
            actor_type=ActorType.USER,
            actor_id=user.id,
            project_id=project_id,
            entity_type="marketing_strategy",
            entity_id=strategy.id,
            data={"version_no": strategy.version_no},
        )
        return strategy

    # --- Контент-план -------------------------------------------------------------------------

    async def latest_plan(self, project_id: UUID) -> PlanView | None:
        plan = await self._session.scalar(
            select(ContentPlan)
            .where(ContentPlan.project_id == project_id, ContentPlan.status != DocumentStatus.ARCHIVED)
            .order_by(ContentPlan.created_at.desc(), ContentPlan.id.desc())
            .limit(1)
        )
        return await self.plan_view(plan) if plan is not None else None

    async def plan_view(self, plan: ContentPlan) -> PlanView:
        items = list(
            await self._session.scalars(
                select(Publication)
                .where(Publication.content_plan_id == plan.id)
                .order_by(Publication.planned_at, Publication.id)
            )
        )
        written = set(
            await self._session.scalars(
                select(PublicationVariant.publication_id)
                .join(PublicationVersion, PublicationVersion.variant_id == PublicationVariant.id)
                .where(PublicationVariant.publication_id.in_([i.id for i in items]))
            )
        )
        return PlanView(plan=plan, items=items, written=written)

    async def get_plan(self, project_id: UUID, plan_id: UUID) -> ContentPlan:
        plan = await self._session.get(ContentPlan, plan_id)
        if plan is None or plan.project_id != project_id:
            raise NotFoundError("Контент-план не найден")
        return plan

    async def approve_plan(self, user: User, project_id: UUID, plan_id: UUID) -> ContentPlan:
        plan = await self.get_plan(project_id, plan_id)
        if plan.status == DocumentStatus.ARCHIVED:
            raise DomainValidationError("Это устаревший вариант плана — откройте актуальный: /plan")
        if plan.status == DocumentStatus.APPROVED:
            return plan
        plan.status = DocumentStatus.APPROVED
        plan.approved_at = datetime.now(UTC)
        await self._session.flush()
        await self._audit.record(
            action="content_plan.approved",
            actor_type=ActorType.USER,
            actor_id=user.id,
            project_id=project_id,
            entity_type="content_plan",
            entity_id=plan.id,
        )
        return plan
