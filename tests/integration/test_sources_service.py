import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import DomainValidationError
from app.db.models import AuditLog, StoredFile
from app.domain.enums import DocumentType, SourceStatus
from app.integrations.storage.local import LocalFileStorage
from app.services.access import AccessService
from app.services.sources import SourceService
from tests.integration.helpers import docx_bytes, make_user

pytestmark = pytest.mark.integration


async def setup(session: AsyncSession, settings: Settings, telegram_id: int):
    user = await make_user(session, telegram_id)
    project = await AccessService(session, default_timezone="Europe/Moscow").create_project(user, "Кофейня")
    service = SourceService(session, LocalFileStorage(settings.files_dir), max_upload_bytes=1024 * 1024)
    return user, project, service


async def test_website_is_added_once(session: AsyncSession, test_settings: Settings) -> None:
    user, project, service = await setup(session, test_settings, 4001)

    site, created = await service.add_website(user, project, "Coffee.ru/menu#x")
    again, created_again = await service.add_website(user, project, "https://coffee.ru/menu")

    assert created is True and created_again is False
    assert site.id == again.id
    assert site.url == "https://coffee.ru/menu"
    assert site.status == SourceStatus.PENDING
    audit = await session.scalar(select(AuditLog).where(AuditLog.action == "source.website_added"))
    assert audit is not None


async def test_internal_website_is_rejected(session: AsyncSession, test_settings: Settings) -> None:
    user, project, service = await setup(session, test_settings, 4002)
    with pytest.raises(DomainValidationError):
        await service.add_website(user, project, "http://169.254.169.254/latest")


async def test_document_is_stored_detected_and_deduplicated(
    session: AsyncSession, test_settings: Settings
) -> None:
    user, project, service = await setup(session, test_settings, 4003)
    data = docx_bytes()

    document, created = await service.add_document(user, project, filename="Услуги.docx", data=data)
    duplicate, created_again = await service.add_document(user, project, filename="копия.docx", data=data)

    assert created is True and created_again is False
    assert duplicate.id == document.id
    assert document.doc_type == DocumentType.DOCX
    assert document.title == "Услуги.docx"
    stored = await session.get(StoredFile, document.file_id)
    assert stored is not None
    assert stored.size_bytes == len(data)
    assert (test_settings.files_dir / stored.storage_key).read_bytes() == data
    assert stored.storage_key.startswith(f"projects/{project.id}/uploads/")


async def test_too_large_or_unsupported_documents_are_rejected(
    session: AsyncSession, test_settings: Settings
) -> None:
    user, project, service = await setup(session, test_settings, 4004)
    with pytest.raises(DomainValidationError, match="больше 1 МБ"):
        await service.add_document(user, project, filename="big.pdf", data=b"%PDF-" + b"0" * (1024 * 1024))
    with pytest.raises(DomainValidationError):
        await service.add_document(user, project, filename="virus.exe", data=b"MZ\x90\x00\x03\x00\xff\xff")


async def test_summary_counts_sources(session: AsyncSession, test_settings: Settings) -> None:
    user, project, service = await setup(session, test_settings, 4005)
    await service.add_website(user, project, "coffee.ru")
    await service.add_document(user, project, filename="Меню.pdf", data=b"%PDF-1.7 menu")
    await service.add_note(user, project, "Мы открыты с 8 утра")

    summary = await service.summary(project.id)

    assert (summary.websites, summary.documents, summary.notes) == (1, 1, 1)
    assert summary.total == 3
    assert "https://coffee.ru/" in summary.recent_titles
    assert "Меню.pdf" in summary.recent_titles
