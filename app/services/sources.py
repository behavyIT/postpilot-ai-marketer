"""Приём источников знаний о бизнесе: сайты, документы, текст из чата.

На этом этапе источники сохраняются со статусом pending; извлечение текста
и построение профиля бизнеса — Этап 4.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainValidationError
from app.core.ids import uuid7
from app.db.models import BusinessDocument, BusinessNote, Project, StoredFile, User, Website
from app.domain.enums import ActorType, FileKind
from app.integrations.documents.detect import detect_file
from app.integrations.storage.base import FileStorage
from app.integrations.web.urls import normalize_site_url
from app.services.audit import AuditService

MAX_NOTE_LENGTH = 4000
MAX_TITLE_LENGTH = 255


@dataclass(frozen=True, slots=True)
class SourcesSummary:
    documents: int
    websites: int
    notes: int
    recent_titles: list[str]

    @property
    def total(self) -> int:
        return self.documents + self.websites + self.notes


class SourceService:
    def __init__(self, session: AsyncSession, storage: FileStorage, *, max_upload_bytes: int) -> None:
        self._session = session
        self._storage = storage
        self._audit = AuditService(session)
        self._max_upload_bytes = max_upload_bytes

    async def add_website(self, user: User, project: Project, raw_url: str) -> tuple[Website, bool]:
        """Добавить сайт. Повторная ссылка не дублируется: возвращается (сайт, False)."""
        url = normalize_site_url(raw_url)
        existing = await self._session.scalar(
            select(Website).where(Website.project_id == project.id, Website.url == url)
        )
        if existing is not None:
            return existing, False
        website = Website(project_id=project.id, url=url, created_by_user_id=user.id)
        self._session.add(website)
        await self._session.flush()
        await self._audit.record(
            action="source.website_added",
            actor_type=ActorType.USER,
            actor_id=user.id,
            project_id=project.id,
            entity_type="website",
            entity_id=website.id,
            data={"url": url},
        )
        return website, True

    async def add_document(
        self, user: User, project: Project, *, filename: str | None, data: bytes
    ) -> tuple[BusinessDocument, bool]:
        """Сохранить документ. Тип определяется по содержимому; одинаковые файлы не дублируются."""
        if len(data) > self._max_upload_bytes:
            limit_mb = self._max_upload_bytes // (1024 * 1024)
            raise DomainValidationError(f"Файл больше {limit_mb} МБ — пришлите файл поменьше")
        detected = detect_file(data)
        digest = hashlib.sha256(data).hexdigest()

        existing = await self._session.scalar(
            select(BusinessDocument).where(
                BusinessDocument.project_id == project.id, BusinessDocument.sha256 == digest
            )
        )
        if existing is not None:
            return existing, False

        file_id = uuid7()
        key = f"projects/{project.id}/uploads/{file_id}.{detected.extension}"
        await self._storage.save(key, data)

        title = _clean_title(filename) or f"Документ {detected.extension.upper()}"
        stored = StoredFile(
            id=file_id,
            project_id=project.id,
            kind=FileKind.UPLOAD,
            storage_backend=self._storage.backend,
            storage_key=key,
            mime_type=detected.mime_type,
            size_bytes=len(data),
            sha256=digest,
            original_name=title,
        )
        document = BusinessDocument(
            project_id=project.id,
            file_id=file_id,
            doc_type=detected.doc_type,
            sha256=digest,
            title=title,
            created_by_user_id=user.id,
        )
        try:
            # Порядок важен: документ ссылается на файл, а relationship между ними нет,
            # поэтому SQLAlchemy сама не упорядочит вставки.
            self._session.add(stored)
            await self._session.flush()
            self._session.add(document)
            await self._session.flush()
        except Exception:
            await self._storage.delete(key)  # не оставляем «осиротевший» файл
            raise
        await self._audit.record(
            action="source.document_added",
            actor_type=ActorType.USER,
            actor_id=user.id,
            project_id=project.id,
            entity_type="business_document",
            entity_id=document.id,
            data={"title": title, "doc_type": detected.doc_type, "size_bytes": len(data)},
        )
        return document, True

    async def add_note(
        self, user: User, project: Project, text: str, *, telegram_message_id: int | None = None
    ) -> BusinessNote:
        clean = text.strip()
        if not clean:
            raise DomainValidationError("Пустое сообщение")
        if len(clean) > MAX_NOTE_LENGTH:
            raise DomainValidationError(
                f"Сообщение длиннее {MAX_NOTE_LENGTH} символов — разбейте на части или пришлите файлом"
            )
        note = BusinessNote(
            project_id=project.id,
            text=clean,
            telegram_message_id=telegram_message_id,
            created_by_user_id=user.id,
        )
        self._session.add(note)
        await self._session.flush()
        return note

    async def summary(self, project_id: UUID) -> SourcesSummary:
        documents = await self._count(BusinessDocument, project_id)
        websites = await self._count(Website, project_id)
        notes = await self._count(BusinessNote, project_id)
        titles = list(
            await self._session.scalars(
                select(BusinessDocument.title)
                .where(BusinessDocument.project_id == project_id)
                .order_by(BusinessDocument.created_at.desc())
                .limit(5)
            )
        )
        sites = list(
            await self._session.scalars(
                select(Website.url)
                .where(Website.project_id == project_id)
                .order_by(Website.created_at.desc())
                .limit(5)
            )
        )
        return SourcesSummary(
            documents=documents, websites=websites, notes=notes, recent_titles=sites + titles
        )

    async def _count(
        self, model: type[BusinessDocument] | type[Website] | type[BusinessNote], project_id: UUID
    ) -> int:
        result = await self._session.scalar(
            select(func.count()).select_from(model).where(model.project_id == project_id)
        )
        return int(result or 0)


def _clean_title(filename: str | None) -> str | None:
    if not filename:
        return None
    clean = " ".join(filename.replace("/", " ").replace("\\", " ").split())
    return clean[:MAX_TITLE_LENGTH] or None
