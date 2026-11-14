"""Определение типа файла по содержимому, а не по расширению.

Расширение и MIME-тип от клиента можно подделать, поэтому смотрим на сигнатуру:
PDF, DOCX и PPTX (zip с характерной структурой), PNG, JPEG, WEBP и обычный текст.
Остальное отклоняется с понятным объяснением.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass

from app.core.errors import DomainValidationError
from app.domain.enums import DocumentType

SUPPORTED_FORMATS_HINT = "PDF, DOCX, PPTX, TXT, а также изображения PNG, JPEG и WEBP"


@dataclass(frozen=True, slots=True)
class DetectedFile:
    doc_type: DocumentType
    mime_type: str
    extension: str


def detect_file(data: bytes) -> DetectedFile:
    if not data:
        raise DomainValidationError("Файл пустой")

    head = data[:16]
    if head.startswith(b"%PDF-"):
        return DetectedFile(DocumentType.PDF, "application/pdf", "pdf")
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return DetectedFile(DocumentType.IMAGE, "image/png", "png")
    if head.startswith(b"\xff\xd8\xff"):
        return DetectedFile(DocumentType.IMAGE, "image/jpeg", "jpg")
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return DetectedFile(DocumentType.IMAGE, "image/webp", "webp")
    if head.startswith(b"PK\x03\x04"):
        return _detect_office(data)
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        raise DomainValidationError(
            "Это старый формат Office (.doc или .ppt) — сохраните файл как DOCX, PPTX или PDF"
        )
    if _looks_like_text(data):
        return DetectedFile(DocumentType.TXT, "text/plain", "txt")
    raise DomainValidationError(f"Такой формат я пока не читаю. Поддерживаются: {SUPPORTED_FORMATS_HINT}")


def _detect_office(data: bytes) -> DetectedFile:
    try:
        # Читается только оглавление архива — распаковки нет, поэтому zip-бомба здесь не опасна.
        names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
    except zipfile.BadZipFile as exc:
        raise DomainValidationError("Файл повреждён или это не документ Office") from exc
    if "[Content_Types].xml" in names and "word/document.xml" in names:
        return DetectedFile(
            DocumentType.DOCX,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "docx",
        )
    if "[Content_Types].xml" in names and "ppt/presentation.xml" in names:
        return DetectedFile(
            DocumentType.PPTX,
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "pptx",
        )
    raise DomainValidationError(f"Этот архив не похож на документ. Поддерживаются: {SUPPORTED_FORMATS_HINT}")


def _looks_like_text(data: bytes) -> bool:
    sample = data[:8192]
    if b"\x00" in sample:
        return False
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            sample.decode(encoding)
        except UnicodeDecodeError:
            continue
        return True
    return False
