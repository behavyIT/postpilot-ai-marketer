import io
import zipfile

import pytest

from app.core.errors import DomainValidationError
from app.domain.enums import DocumentType
from app.integrations.documents.detect import detect_file


def office_zip(*names: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(name, "<xml/>")
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("data", "doc_type", "mime"),
    [
        (b"%PDF-1.7\n...", DocumentType.PDF, "application/pdf"),
        (b"\x89PNG\r\n\x1a\n" + b"0" * 20, DocumentType.IMAGE, "image/png"),
        (b"\xff\xd8\xff\xe0" + b"0" * 20, DocumentType.IMAGE, "image/jpeg"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", DocumentType.IMAGE, "image/webp"),
        ("Кофейня «Зерно»: меню и цены".encode(), DocumentType.TXT, "text/plain"),
        ("Прайс-лист".encode("cp1251"), DocumentType.TXT, "text/plain"),
    ],
)
def test_supported_formats_are_detected_by_content(data: bytes, doc_type: DocumentType, mime: str) -> None:
    detected = detect_file(data)
    assert detected.doc_type is doc_type
    assert detected.mime_type == mime


def test_docx_and_pptx_are_told_apart() -> None:
    docx = detect_file(office_zip("[Content_Types].xml", "word/document.xml"))
    pptx = detect_file(office_zip("[Content_Types].xml", "ppt/presentation.xml"))
    assert docx.doc_type is DocumentType.DOCX
    assert pptx.doc_type is DocumentType.PPTX


@pytest.mark.parametrize(
    ("data", "message_part"),
    [
        (b"", "пустой"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "старый формат"),
        (office_zip("[Content_Types].xml", "xl/workbook.xml"), "не похож на документ"),
        (b"PK\x03\x04broken", "повреждён"),
        (b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff", "не читаю"),
    ],
)
def test_unsupported_files_are_rejected_with_explanation(data: bytes, message_part: str) -> None:
    with pytest.raises(DomainValidationError) as error:
        detect_file(data)
    assert message_part in str(error.value)
