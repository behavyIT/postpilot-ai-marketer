"""История переписки пользователя с ботом.

Это не «память» модели, а журнал: из него берутся последние реплики как контекст
для распознавания намерений («да», «на неделю» после вопроса бота).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from app.domain.enums import MessageRole, check_values


class ConversationMessage(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (
        CheckConstraint(f"role IN ({check_values(MessageRole)})", name="role"),
        Index("ix_conversation_messages_user_id_created_at", "user_id", "created_at"),
    )

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    intent: Mapped[str | None] = mapped_column(String(32))
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
