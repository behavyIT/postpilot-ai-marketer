"""Стратегия, контент-план, публикации и память агента.

Публикация — логический пост (тема, цель, рубрика, дата). Под каждую площадку у неё
свой вариант со своим статусом, а тексты варианта хранятся неизменяемыми версиями:
любая правка (текст или картинка) создаёт новую версию, и согласование всегда относится
к конкретной.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.domain.enums import (
    ActorType,
    ContentGoal,
    DocumentStatus,
    MemoryKind,
    Platform,
    PublicationStatus,
    check_values,
)


class MarketingStrategy(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "marketing_strategies"
    __table_args__ = (
        CheckConstraint(f"status IN ({check_values(DocumentStatus)})", name="status"),
        UniqueConstraint("project_id", "version_no"),
        # Утверждённая стратегия у проекта может быть только одна.
        Index(
            "uq_marketing_strategies_one_approved",
            "project_id",
            unique=True,
            postgresql_where=text("status = 'approved'"),
        ),
    )

    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    version_no: Mapped[int]
    status: Mapped[str] = mapped_column(String(16), default=DocumentStatus.DRAFT)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    instructions: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class ContentPlan(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "content_plans"
    __table_args__ = (
        CheckConstraint(f"status IN ({check_values(DocumentStatus)})", name="status"),
        CheckConstraint("period_end >= period_start", name="period"),
        Index("ix_content_plans_project_id_period_start", "project_id", "period_start"),
    )

    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    strategy_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("marketing_strategies.id", ondelete="SET NULL")
    )
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default=DocumentStatus.DRAFT)
    instructions: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    created_by_type: Mapped[str] = mapped_column(String(16), default=ActorType.USER)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Publication(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Пункт контент-плана или разовый пост."""

    __tablename__ = "publications"
    __table_args__ = (
        CheckConstraint(f"goal IN ({check_values(ContentGoal)})", name="goal"),
        Index("ix_publications_project_id_planned_at", "project_id", "planned_at"),
        Index("ix_publications_content_plan_id", "content_plan_id"),
    )

    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    content_plan_id: Mapped[UUID | None] = mapped_column(ForeignKey("content_plans.id", ondelete="CASCADE"))
    planned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    category: Mapped[str] = mapped_column(String(80))
    goal: Mapped[str] = mapped_column(String(16))
    topic: Mapped[str] = mapped_column(String(300))
    title: Mapped[str] = mapped_column(String(300))
    brief: Mapped[str] = mapped_column(Text)
    cta: Mapped[str | None] = mapped_column(String(300))
    hashtags: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    visual_idea: Mapped[str | None] = mapped_column(Text)


class PublicationVariant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Публикация, адаптированная под площадку. Статус живёт здесь."""

    __tablename__ = "publication_variants"
    __table_args__ = (
        CheckConstraint(f"platform IN ({check_values(Platform)})", name="platform"),
        CheckConstraint(f"status IN ({check_values(PublicationStatus)})", name="status"),
        UniqueConstraint("publication_id", "platform"),
        Index(
            "ix_publication_variants_scheduled_at",
            "scheduled_at",
            postgresql_where=text("status = 'scheduled'"),
        ),
    )

    publication_id: Mapped[UUID] = mapped_column(ForeignKey("publications.id", ondelete="CASCADE"))
    platform: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24), default=PublicationStatus.DRAFT)
    current_version_no: Mapped[int] = mapped_column(Integer, default=0)
    approved_version_no: Mapped[int | None]
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_post_id: Mapped[str | None] = mapped_column(String(128))
    external_url: Mapped[str | None] = mapped_column(String(512))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    # Куда публиковать — фиксируется при постановке в расписание.
    social_account_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("social_accounts.id", ondelete="SET NULL")
    )


class PublicationVersion(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Неизменяемый текст варианта. Правка = новая версия."""

    __tablename__ = "publication_versions"
    __table_args__ = (
        UniqueConstraint("variant_id", "version_no"),
        CheckConstraint(f"created_by_type IN ({check_values(ActorType)})", name="created_by_type"),
    )

    variant_id: Mapped[UUID] = mapped_column(ForeignKey("publication_variants.id", ondelete="CASCADE"))
    version_no: Mapped[int]
    body: Mapped[str] = mapped_column(Text)
    hashtags: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    content_hash: Mapped[str] = mapped_column(String(64))
    created_by_type: Mapped[str] = mapped_column(String(16), default=ActorType.AI)
    change_request: Mapped[str | None] = mapped_column(Text)
    used_fact_ids: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    warnings: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    # Картинка — часть версии: её замена тоже требует нового одобрения.
    media_file_id: Mapped[UUID | None] = mapped_column(ForeignKey("stored_files.id", ondelete="SET NULL"))
    image_prompt: Mapped[str | None] = mapped_column(Text)


class AgentMemory(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Структурированная память: пожелания владельца, которые учитываются дальше."""

    __tablename__ = "agent_memory"
    __table_args__ = (
        CheckConstraint(f"kind IN ({check_values(MemoryKind)})", name="kind"),
        Index("ix_agent_memory_project_id_kind", "project_id", "kind"),
    )

    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(32))  # откуда взято: strategy, plan, post
    active: Mapped[bool] = mapped_column(default=True, server_default="true")
