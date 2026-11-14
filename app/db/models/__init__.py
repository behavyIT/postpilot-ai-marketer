"""Все модели импортируются здесь, чтобы Alembic видел полную схему."""

from app.db.models.audit import AuditLog
from app.db.models.content import (
    AgentMemory,
    ContentPlan,
    MarketingStrategy,
    Publication,
    PublicationVariant,
    PublicationVersion,
)
from app.db.models.conversation import ConversationMessage
from app.db.models.llm_usage import LLMUsage
from app.db.models.profile import BusinessProfile, BusinessProfileVersion, ProfileFact
from app.db.models.project import Project, ProjectMember
from app.db.models.source import BusinessDocument, BusinessNote, StoredFile, Website, WebsitePage
from app.db.models.user import User

__all__ = [
    "AgentMemory",
    "AuditLog",
    "BusinessDocument",
    "BusinessNote",
    "BusinessProfile",
    "BusinessProfileVersion",
    "ContentPlan",
    "ConversationMessage",
    "LLMUsage",
    "MarketingStrategy",
    "ProfileFact",
    "Project",
    "ProjectMember",
    "Publication",
    "PublicationVariant",
    "PublicationVersion",
    "StoredFile",
    "User",
    "Website",
    "WebsitePage",
]
