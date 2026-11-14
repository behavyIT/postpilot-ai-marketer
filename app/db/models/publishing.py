"""Согласование и публикация: подключённые каналы, решения, журнал переходов, попытки отправки.

Защита от двойной публикации опирается на эти таблицы: решение об одобрении привязано
к версии и хешу содержимого, а успешная попытка по варианту может быть только одна
(частичный уникальный индекс).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
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
    ApprovalDecision,
    Platform,
    PublicationStatus,
    PublishAttemptStatus,
    SocialAccountStatus,
    check_values,
)


class SocialAccount(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Площадка проекта, куда можно публиковать: Telegram-канал, сообщество VK."""

    __tablename__ = "social_accounts"
    __table_args__ = (
        CheckConstraint(f"platform IN ({check_values(Platform)})", name="platform"),
        CheckConstraint(f"status IN ({check_values(SocialAccountStatus)})", name="status"),
        UniqueConstraint("project_id", "platform", "external_id"),
    )

    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    platform: Mapped[str] = mapped_column(String(16))
    external_id: Mapped[str] = mapped_column(String(64))  # chat_id канала
    title: Mapped[str] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default=SocialAccountStatus.ACTIVE)
    can_post: Mapped[bool] = mapped_column(default=False)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    connected_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class Approval(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Решение владельца по версии. Только добавление: история решений не переписывается."""

    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(f"decision IN ({check_values(ApprovalDecision)})", name="decision"),
        Index("ix_approvals_variant_id_created_at", "variant_id", "created_at"),
    )

    variant_id: Mapped[UUID] = mapped_column(ForeignKey("publication_variants.id", ondelete="CASCADE"))
    version_no: Mapped[int]
    decision: Mapped[str] = mapped_column(String(24))
    content_hash: Mapped[str] = mapped_column(String(64))
    decided_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    comment: Mapped[str | None] = mapped_column(Text)


class PublicationEvent(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Журнал переходов статуса варианта: кто, когда, откуда, куда и почему."""

    __tablename__ = "publication_events"
    __table_args__ = (
        CheckConstraint(f"to_status IN ({check_values(PublicationStatus)})", name="to_status"),
        CheckConstraint(f"actor_type IN ({check_values(ActorType)})", name="actor_type"),
        Index("ix_publication_events_variant_id_created_at", "variant_id", "created_at"),
    )

    variant_id: Mapped[UUID] = mapped_column(ForeignKey("publication_variants.id", ondelete="CASCADE"))
    from_status: Mapped[str | None] = mapped_column(String(24))
    to_status: Mapped[str] = mapped_column(String(24))
    actor_type: Mapped[str] = mapped_column(String(16))
    actor_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reason: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")


class PublishAttempt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Попытка отправить версию на площадку. Создаётся и фиксируется ДО вызова API."""

    __tablename__ = "publish_attempts"
    __table_args__ = (
        CheckConstraint(f"status IN ({check_values(PublishAttemptStatus)})", name="status"),
        UniqueConstraint("idempotency_key", "attempt_no"),
        # Второй успешной публикации одного варианта база не допустит.
        Index(
            "uq_publish_attempts_one_success",
            "variant_id",
            unique=True,
            postgresql_where=text("status = 'succeeded'"),
        ),
    )

    variant_id: Mapped[UUID] = mapped_column(ForeignKey("publication_variants.id", ondelete="CASCADE"))
    version_no: Mapped[int]
    # Каналы не удаляются (только отключаются); удаляются вместе с проектом — и история попыток с ними.
    social_account_id: Mapped[UUID] = mapped_column(ForeignKey("social_accounts.id", ondelete="CASCADE"))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    attempt_no: Mapped[int]
    status: Mapped[str] = mapped_column(String(24), default=PublishAttemptStatus.STARTED)
    external_post_id: Mapped[str | None] = mapped_column(String(128))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PostMessage(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Сообщение бота с превью поста. Ответ на него — правка именно этого поста."""

    __tablename__ = "post_messages"
    __table_args__ = (
        UniqueConstraint("chat_id", "message_id"),
        Index("ix_post_messages_chat_id_created_at", "chat_id", "created_at"),
    )

    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    variant_id: Mapped[UUID] = mapped_column(ForeignKey("publication_variants.id", ondelete="CASCADE"))
    version_no: Mapped[int]
