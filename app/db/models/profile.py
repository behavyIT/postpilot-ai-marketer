"""Профиль бизнеса: факты с цитатами из источников и версии профиля."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.domain.enums import ActorType, FactStatus, ProfileField, SourceKind, check_values


class ProfileFact(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Факт о бизнесе. quote — дословная цитата из источника, по ней факт проверяется."""

    __tablename__ = "profile_facts"
    __table_args__ = (
        CheckConstraint(f"field IN ({check_values(ProfileField)})", name="field"),
        CheckConstraint(f"source_kind IN ({check_values(SourceKind)})", name="source_kind"),
        CheckConstraint("status IN ('confirmed', 'inferred')", name="status"),
        Index("ix_profile_facts_project_id_field", "project_id", "field"),
        Index("ix_profile_facts_source", "source_kind", "source_id"),
    )

    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    field: Mapped[str] = mapped_column(String(40))
    value: Mapped[str] = mapped_column(Text)
    quote: Mapped[str] = mapped_column(Text)
    source_kind: Mapped[str] = mapped_column(String(16))
    source_id: Mapped[UUID]
    source_label: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(16), default=FactStatus.INFERRED)


class BusinessProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "business_profiles"
    __table_args__ = (UniqueConstraint("project_id"),)

    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    latest_version_no: Mapped[int] = mapped_column(default=0)
    completeness: Mapped[int] = mapped_column(default=0)  # % заполненных пунктов из 14


class BusinessProfileVersion(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Неизменяемый снимок профиля. data — BusinessProfileData в JSON."""

    __tablename__ = "business_profile_versions"
    __table_args__ = (
        UniqueConstraint("profile_id", "version_no"),
        CheckConstraint(f"created_by_type IN ({check_values(ActorType)})", name="created_by_type"),
    )

    profile_id: Mapped[UUID] = mapped_column(ForeignKey("business_profiles.id", ondelete="CASCADE"))
    version_no: Mapped[int]
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    questions: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    created_by_type: Mapped[str] = mapped_column(String(16), default=ActorType.AI)
    change_summary: Mapped[str | None] = mapped_column(Text)
