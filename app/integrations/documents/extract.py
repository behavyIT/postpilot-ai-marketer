"""Извлечение текста из документов: отдельный обработчик на каждый формат.

Функции синхронные и ресурсоёмкие — вызывать через asyncio.to_thread.
Изображения здесь не распознаются: для них текст получает мультимодальная модель.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field

import pypdfium2
from charset_normalizer import from_bytes
from docx import Document as load_docx
from pptx import Presentation as load_pptx

from app.core.errors import DomainValidationError
from app.domain.enums import DocumentType

MAX_TEXT_CHARS = 200_000
MAX_PDF_PAGES = 100
MAX_UNZIPPED_BYTES = 100 * 1024 * 1024  # защита от zip-бомб в DOCX/PPTX
MAX_COMPRESSION_RATIO = 100
SCANNED_PDF_THRESHOLD = 50  # меньше символов на документ — вероятно, скан без текстового слоя


@dataclass(slots=True)
class ExtractedText:
    text: str
    page_count: int | None = None
    warnings: list[str] = field(default_factory=list)


def extract_text(doc_type: DocumentType, data: bytes) -> ExtractedText:
    handlers: dict[DocumentType, Callable[[bytes], ExtractedText]] = {
        DocumentType.PDF: _extract_pdf,
        DocumentType.DOCX: _extract_docx,
        DocumentType.PPTX: _extract_pptx,
        DocumentType.TXT: _extract_txt,
    }
    handler = handlers.get(doc_type)
    if handler is None:
        raise DomainValidationError(f"Для типа {doc_type} текст извлекается моделью, а не парсером")
    try:
        result = handler(data)
    except DomainValidationError:
        raise
    except Exception as exc:
        # Библиотеки разбора падают на испорченных файлах самыми разными исключениями
        # (AttributeError, KeyError, lxml...). Один битый файл не должен ронять весь анализ.
        raise DomainValidationError("Не удалось прочитать документ — файл повреждён") from exc
    if len(result.text) > MAX_TEXT_CHARS:
        result.text = result.text[:MAX_TEXT_CHARS]
        result.warnings.append(f"Текст обрезан до {MAX_TEXT_CHARS} символов")
    return result


def _extract_pdf(data: bytes) -> ExtractedText:
    try:
        pdf = pypdfium2.PdfDocument(data)
    except pypdfium2.PdfiumError as exc:
        raise DomainValidationError("PDF повреждён или защищён паролем") from exc
    try:
        total = len(pdf)
        pages: list[str] = []
        for index in range(min(total, MAX_PDF_PAGES)):
            page = pdf[index]
            textpage = page.get_textpage()
            try:
                content = textpage.get_text_range().strip()
            finally:
                textpage.close()
                page.close()
            if content:
                pages.append(f"[Страница {index + 1}]\n{content}")
    finally:
        pdf.close()

    result = ExtractedText(text="\n\n".join(pages), page_count=total)
    if total > MAX_PDF_PAGES:
        result.warnings.append(f"Прочитаны первые {MAX_PDF_PAGES} страниц из {total}")
    if len(result.text) < SCANNED_PDF_THRESHOLD:
        result.warnings.append("В PDF почти нет текста — возможно, это скан. Пришлите текстовую версию")
    return result


def _check_zip(data: bytes) -> None:
    try:
        infos = zipfile.ZipFile(io.BytesIO(data)).infolist()
    except zipfile.BadZipFile as exc:
        raise DomainValidationError("Файл повреждён") from exc
    unzipped = sum(info.file_size for info in infos)
    if unzipped > MAX_UNZIPPED_BYTES or unzipped > len(data) * MAX_COMPRESSION_RATIO:
        raise DomainValidationError("Документ подозрительно сильно сжат — не открываю его")


def _extract_docx(data: bytes) -> ExtractedText:
    _check_zip(data)
    document = load_docx(io.BytesIO(data))
    parts = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(dict.fromkeys(cells)))  # объединённые ячейки повторяются
    return ExtractedText(text="\n".join(parts))


def _extract_pptx(data: bytes) -> ExtractedText:
    _check_zip(data)
    presentation = load_pptx(io.BytesIO(data))
    slides: list[str] = []
    for number, slide in enumerate(presentation.slides, start=1):
        lines: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                lines.append(shape.text_frame.text.strip())
            if shape.has_table:
                for row in shape.table.rows:
                    cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                    if cells:
                        lines.append(" | ".join(cells))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                lines.append(f"Заметки: {notes}")
        if lines:
            slides.append(f"[Слайд {number}]\n" + "\n".join(lines))
    return ExtractedText(text="\n\n".join(slides), page_count=len(presentation.slides))


def _extract_txt(data: bytes) -> ExtractedText:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return ExtractedText(text=data.decode(encoding).strip())
        except UnicodeDecodeError:
            continue
    best = from_bytes(data).best()
    if best is None:
        raise DomainValidationError("Не удалось определить кодировку текста")
    return ExtractedText(text=str(best).strip())
