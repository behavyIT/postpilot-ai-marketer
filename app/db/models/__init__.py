"""Все модели импортируются здесь, чтобы Alembic видел полную схему."""

from app.db.models.audit import AuditLog
from app.db.models.conversation import ConversationMessage
from app.db.models.llm_usage import LLMUsage
from app.db.models.profile import BusinessProfile, BusinessProfileVersion, ProfileFact
from app.db.models.project import Project, ProjectMember
from app.db.models.source import BusinessDocument, BusinessNote, StoredFile, Website, WebsitePage
from app.db.models.user import User

__all__ = [
    "AuditLog",
    "BusinessDocument",
    "BusinessNote",
    "BusinessProfile",
    "BusinessProfileVersion",
    "ConversationMessage",
    "LLMUsage",
    "ProfileFact",
    "Project",
    "ProjectMember",
    "StoredFile",
    "User",
    "Website",
    "WebsitePage",
]
