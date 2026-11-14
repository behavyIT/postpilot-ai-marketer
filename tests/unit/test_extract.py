import io
import zipfile

import pypdfium2
import pytest
from docx import Document
from pptx import Presentation

from app.core.errors import DomainValidationError
from app.domain.enums import DocumentType
from app.integrations.documents.extract import extract_text


def test_docx_paragraphs_and_tables() -> None:
    document = Document()
    document.add_paragraph("Кофейня «Зерно»: средний чек 450 ₽")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Капучино"
    table.cell(0, 1).text = "250 ₽"
    buffer = io.BytesIO()
    document.save(buffer)

    result = extract_text(DocumentType.DOCX, buffer.getvalue())

    assert "средний чек 450 ₽" in result.text
    assert "Капучино | 250 ₽" in result.text


def test_pptx_slides_with_numbers() -> None:
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    slide.shapes.title.text = "О нас"
    slide.placeholders[1].text = "Варим спешелти с 2015 года"
    buffer = io.BytesIO()
    presentation.save(buffer)

    result = extract_text(DocumentType.PPTX, buffer.getvalue())

    assert result.page_count == 1
    assert result.text.startswith("[Слайд 1]")
    assert "Варим спешелти с 2015 года" in result.text


def test_pdf_without_text_is_reported_as_scan() -> None:
    pdf = pypdfium2.PdfDocument.new()
    pdf.new_page(200, 200)
    buffer = io.BytesIO()
    pdf.save(buffer)

    result = extract_text(DocumentType.PDF, buffer.getvalue())

    assert result.text == ""
    assert result.page_count == 1
    assert any("скан" in warning for warning in result.warnings)


def test_broken_pdf_gives_friendly_error() -> None:
    with pytest.raises(DomainValidationError, match="PDF"):
        extract_text(DocumentType.PDF, b"%PDF-1.7 broken")


def test_corrupted_office_file_gives_friendly_error() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")  # проходит проверку формата, но внутри мусор
        archive.writestr("word/document.xml", "<w:document/>")
    with pytest.raises(DomainValidationError, match="повреждён"):
        extract_text(DocumentType.DOCX, buffer.getvalue())


def test_zip_bomb_is_rejected() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", b"0" * (20 * 1024 * 1024))
    with pytest.raises(DomainValidationError, match="сжат"):
        extract_text(DocumentType.DOCX, buffer.getvalue())


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "cp1251"])
def test_txt_encodings(encoding: str) -> None:
    assert (
        extract_text(DocumentType.TXT, "Прайс-лист на ноябрь".encode(encoding)).text == "Прайс-лист на ноябрь"
    )


def test_images_are_not_parsed_locally() -> None:
    with pytest.raises(DomainValidationError):
        extract_text(DocumentType.IMAGE, b"\x89PNG")
