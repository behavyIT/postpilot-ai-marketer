"""Учёт обращений к языковым моделям: токены, задержка, результат."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from app.domain.enums import LLMCallStatus, check_values


class LLMUsage(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "llm_usage"
    __table_args__ = (
        CheckConstraint(f"status IN ({check_values(LLMCallStatus)})", name="status"),
        Index("ix_llm_usage_project_id_created_at", "project_id", "created_at"),
        Index("ix_llm_usage_created_at", "created_at"),
    )

    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    purpose: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16))
    error: Mapped[str | None] = mapped_column(Text)
