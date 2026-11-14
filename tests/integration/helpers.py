"""Общие помощники интеграционных тестов."""

from __future__ import annotations

import io
import zipfile
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.orchestrator.orchestrator import Orchestrator, Turn
from app.ai.llm.base import ModelProfile
from app.ai.llm.fake import FakeLLMProvider
from app.ai.llm.limiter import NullLimiter
from app.ai.llm.service import LLMService
from app.ai.llm.usage import MemoryUsageSink
from app.core.config import Settings
from app.db.models import User
from app.integrations.storage.local import LocalFileStorage
from app.services.access import AccessService, TelegramProfile


async def make_user(session: AsyncSession, telegram_id: int, first_name: str = "Анна") -> User:
    access = AccessService(session, default_timezone="Europe/Moscow")
    user, _ = await access.register_or_update_user(
        TelegramProfile(telegram_user_id=telegram_id, first_name=first_name)
    )
    return user


def intent(name: str, confidence: float = 0.95, **params: Any) -> dict[str, Any]:
    return {"intent": name, "confidence": confidence, **params}


def make_orchestrator(session: AsyncSession, settings: Settings, provider: FakeLLMProvider) -> Orchestrator:
    llm = LLMService(
        provider,
        models={ModelProfile.SMART: "ultra", ModelProfile.FAST: "lite"},
        limiter=NullLimiter(),
        usage=MemoryUsageSink(),
    )
    return Orchestrator(session, llm=llm, storage=LocalFileStorage(settings.files_dir), settings=settings)


async def turn_for(session: AsyncSession, user: User) -> Turn:
    project = await AccessService(session, default_timezone="Europe/Moscow").get_active_project(user)
    return Turn(user=user, project=project, telegram_message_id=1)


def docx_bytes(text: str = "Описание услуг") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", f"<w:document>{text}</w:document>")
    return buffer.getvalue()
