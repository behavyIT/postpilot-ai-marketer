"""Источники знаний о бизнесе: файлы, документы, сайты и заметки из чата.

На Этапе 3 источники только принимаются и сохраняются (статус pending);
извлечение текста и анализ — Этап 4.
"""

from __future__ import annotations

from datetime import datetime
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
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.domain.enums import DocumentType, FileKind, SourceStatus, check_values


class StoredFile(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Файл в хранилище (локальная папка в MVP, S3 — позже)."""

    __tablename__ = "stored_files"
    __table_args__ = (
        CheckConstraint(f"kind IN ({check_values(FileKind)})", name="kind"),
        CheckConstraint("size_bytes >= 0", name="size_non_negative"),
        Index("ix_stored_files_project_id", "project_id"),
    )

    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))
    storage_backend: Mapped[str] = mapped_column(String(16), default="local")
    storage_key: Mapped[str] = mapped_column(String(512), unique=True)
    mime_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    original_name: Mapped[str | None] = mapped_column(String(255))


class BusinessDocument(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "business_documents"
    __table_args__ = (
        CheckConstraint(f"doc_type IN ({check_values(DocumentType)})", name="doc_type"),
        CheckConstraint(f"status IN ({check_values(SourceStatus)})", name="status"),
        # Один и тот же файл в проекте храним один раз.
        UniqueConstraint("project_id", "sha256"),
    )

    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    file_id: Mapped[UUID] = mapped_column(ForeignKey("stored_files.id", ondelete="CASCADE"))
    doc_type: Mapped[str] = mapped_column(String(16))
    sha256: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default=SourceStatus.PENDING)
    error: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class Website(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "websites"
    __table_args__ = (
        CheckConstraint(f"status IN ({check_values(SourceStatus)})", name="status"),
        UniqueConstraint("project_id", "url"),
    )

    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(String(2048))
    status: Mapped[str] = mapped_column(String(16), default=SourceStatus.PENDING)
    error: Mapped[str | None] = mapped_column(Text)
    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class BusinessNote(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Сведения о бизнесе, которые пользователь написал прямо в чате."""

    __tablename__ = "business_notes"
    __table_args__ = (
        CheckConstraint("length(btrim(text)) > 0", name="text_not_blank"),
        Index("ix_business_notes_project_id_created_at", "project_id", "created_at"),
    )

    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    text: Mapped[str] = mapped_column(Text)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
