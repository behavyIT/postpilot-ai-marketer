"""Перечисления предметной области.

В БД они хранятся как text + CHECK-ограничение: такие значения проще мигрировать,
чем типы ENUM в PostgreSQL.
"""

from __future__ import annotations

from enum import StrEnum


class UserStatus(StrEnum):
    ACTIVE = "active"
    BLOCKED = "blocked"


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class AutomationMode(StrEnum):
    OFF = "off"
    ASSISTANT = "assistant"


class ProjectRole(StrEnum):
    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


class ActorType(StrEnum):
    USER = "user"
    SYSTEM = "system"
    AI = "ai"


class FileKind(StrEnum):
    UPLOAD = "upload"
    GENERATED_IMAGE = "generated_image"


class DocumentType(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    PPTX = "pptx"
    TXT = "txt"
    IMAGE = "image"


class SourceStatus(StrEnum):
    """Состояние обработки источника знаний (документа, сайта)."""

    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class LLMCallStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    INVALID_OUTPUT = "invalid_output"


class ProfileField(StrEnum):
    """14 пунктов профиля бизнеса из ТЗ (§2.2)."""

    COMPANY_NAME = "company_name"
    INDUSTRY = "industry"
    PRODUCTS_SERVICES = "products_services"
    TARGET_AUDIENCE = "target_audience"
    GEOGRAPHY = "geography"
    UNIQUE_SELLING_POINTS = "unique_selling_points"
    COMPETITIVE_ADVANTAGES = "competitive_advantages"
    PRICING = "pricing"
    COMMUNICATION_STYLE = "communication_style"
    PRODUCT_BENEFITS = "product_benefits"
    CUSTOMER_PAINS = "customer_pains"
    FAQ = "faq"
    ADVERTISING_RESTRICTIONS = "advertising_restrictions"
    CONTACTS = "contacts"


class SourceKind(StrEnum):
    NOTE = "note"
    WEBSITE_PAGE = "website_page"
    DOCUMENT = "document"


class FactStatus(StrEnum):
    """Насколько можно доверять факту."""

    CONFIRMED = "confirmed"  # сказал сам пользователь
    INFERRED = "inferred"  # найдено в источниках (сайт, документы)
    MISSING = "missing"  # данных нет


def check_values(enum_cls: type[StrEnum]) -> str:
    """SQL-список значений для CHECK-ограничения: 'a', 'b', 'c'."""
    return ", ".join(f"'{member.value}'" for member in enum_cls)
