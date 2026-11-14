"""Проект — один бизнес пользователя, и участники проекта."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.domain.enums import AutomationMode, ProjectRole, ProjectStatus, check_values


class Project(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint(f"status IN ({check_values(ProjectStatus)})", name="status"),
        CheckConstraint(f"automation_mode IN ({check_values(AutomationMode)})", name="automation_mode"),
        CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        Index("ix_projects_status", "status"),
    )

    name: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default=ProjectStatus.ACTIVE)
    automation_mode: Mapped[str] = mapped_column(String(16), default=AutomationMode.ASSISTANT)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProjectMember(CreatedAtMixin, Base):
    """Участник проекта. В MVP у проекта один владелец, но доступ всегда проверяется через членство."""

    __tablename__ = "project_members"
    __table_args__ = (
        CheckConstraint(f"role IN ({check_values(ProjectRole)})", name="role"),
        Index("ix_project_members_user_id", "user_id"),
    )

    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), default=ProjectRole.OWNER)
