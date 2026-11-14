"""Журнал аудита: неизменяемая запись значимых действий."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from app.domain.enums import ActorType, check_values


class AuditLog(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint(f"actor_type IN ({check_values(ActorType)})", name="actor_type"),
        Index("ix_audit_logs_project_id_created_at", "project_id", "created_at"),
    )

    actor_type: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[UUID | None]
    # При удалении проекта запись остаётся, ссылка обнуляется.
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[UUID | None]
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
