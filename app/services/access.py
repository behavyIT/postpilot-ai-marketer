"""Пользователи и проекты: регистрация, создание проектов, проверка доступа.

Все методы, которые возвращают проект, проверяют членство пользователя в нём —
это основа изоляции данных разных пользователей. Транзакцией управляет вызывающий код
(обработчик бота или API): сервис только делает flush.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainValidationError, NotFoundError
from app.db.models import Project, ProjectMember, User
from app.domain.enums import ActorType, ProjectRole, ProjectStatus
from app.services.audit import AuditService

PROJECT_NAME_MAX_LENGTH = 120


@dataclass(frozen=True, slots=True)
class TelegramProfile:
    """Данные пользователя, которые присылает Telegram."""

    telegram_user_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    language_code: str | None = None


class AccessService:
    def __init__(self, session: AsyncSession, *, default_timezone: str) -> None:
        self._session = session
        self._audit = AuditService(session)
        self._default_timezone = default_timezone

    # --- Пользователи -------------------------------------------------------------------------

    async def register_or_update_user(self, profile: TelegramProfile) -> tuple[User, bool]:
        """Создать пользователя при первом обращении или обновить его данные.

        Возвращает (пользователь, создан_ли_сейчас). Безопасно при одновременных
        запросах: вставка идёт через ON CONFLICT DO NOTHING.
        """
        now = datetime.now(UTC)
        inserted_id = await self._session.scalar(
            insert(User)
            .values(
                telegram_user_id=profile.telegram_user_id,
                username=profile.username,
                first_name=profile.first_name,
                last_name=profile.last_name,
                language_code=profile.language_code,
                timezone=self._default_timezone,
                last_seen_at=now,
            )
            .on_conflict_do_nothing(index_elements=[User.telegram_user_id])
            .returning(User.id)
        )
        created = inserted_id is not None

        user = await self._session.scalar(
            select(User)
            .where(User.telegram_user_id == profile.telegram_user_id)
            .execution_options(populate_existing=True)
        )
        if user is None:  # pragma: no cover — строка только что вставлена или уже была
            raise NotFoundError("Пользователь не найден")

        if created:
            await self._audit.record(action="user.registered", actor_type=ActorType.USER, actor_id=user.id)
        else:
            user.username = profile.username
            user.first_name = profile.first_name
            user.last_name = profile.last_name
            user.language_code = profile.language_code
            user.last_seen_at = now
            await self._session.flush()
        return user, created

    async def find_user(self, telegram_user_id: int) -> User | None:
        """Пользователь, который уже писал боту (без регистрации нового)."""
        return await self._session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))

    async def accept_consent(self, user: User) -> None:
        """Зафиксировать согласие на обработку данных (один раз)."""
        if user.consent_at is not None:
            return
        user.consent_at = datetime.now(UTC)
        await self._session.flush()
        await self._audit.record(action="user.consent_accepted", actor_type=ActorType.USER, actor_id=user.id)

    # --- Проекты ------------------------------------------------------------------------------

    async def create_project(self, user: User, name: str, *, timezone: str | None = None) -> Project:
        """Создать проект; пользователь становится владельцем, проект — активным."""
        clean_name = " ".join(name.split())
        if not clean_name:
            raise DomainValidationError("Название проекта не может быть пустым")
        if len(clean_name) > PROJECT_NAME_MAX_LENGTH:
            raise DomainValidationError(
                f"Название проекта длиннее {PROJECT_NAME_MAX_LENGTH} символов — сократите его"
            )
        tz = timezone or user.timezone or self._default_timezone
        _ensure_timezone(tz)

        project = Project(name=clean_name, timezone=tz)
        self._session.add(project)
        await self._session.flush()
        self._session.add(ProjectMember(project_id=project.id, user_id=user.id, role=ProjectRole.OWNER))
        user.active_project_id = project.id
        await self._session.flush()

        await self._audit.record(
            action="project.created",
            actor_type=ActorType.USER,
            actor_id=user.id,
            project_id=project.id,
            entity_type="project",
            entity_id=project.id,
            data={"name": clean_name, "timezone": tz},
        )
        return project

    async def list_projects(self, user: User) -> list[Project]:
        """Проекты, в которых пользователь участвует (без удалённых), от новых к старым."""
        result = await self._session.scalars(
            select(Project)
            .join(ProjectMember, ProjectMember.project_id == Project.id)
            .where(ProjectMember.user_id == user.id, Project.deleted_at.is_(None))
            .order_by(Project.created_at.desc(), Project.id.desc())
        )
        return list(result)

    async def get_project(self, user: User, project_id: UUID) -> Project:
        """Проект пользователя. Для чужого и несуществующего проекта — одинаковая ошибка."""
        project = await self._session.scalar(
            select(Project)
            .join(ProjectMember, ProjectMember.project_id == Project.id)
            .where(
                Project.id == project_id,
                ProjectMember.user_id == user.id,
                Project.deleted_at.is_(None),
            )
        )
        if project is None:
            raise NotFoundError("Проект не найден")
        return project

    async def get_active_project(self, user: User) -> Project | None:
        """Активный проект пользователя, если он задан и всё ещё доступен."""
        if user.active_project_id is None:
            return None
        try:
            return await self.get_project(user, user.active_project_id)
        except NotFoundError:
            user.active_project_id = None
            await self._session.flush()
            return None

    async def set_active_project(self, user: User, project_id: UUID) -> Project:
        project = await self.get_project(user, project_id)
        user.active_project_id = project.id
        await self._session.flush()
        return project

    async def find_projects_by_name(self, user: User, query: str) -> list[Project]:
        """Проекты пользователя, в названии которых есть query (без учёта регистра)."""
        needle = query.casefold().strip()
        if not needle:
            return []
        return [p for p in await self.list_projects(user) if needle in p.name.casefold()]

    async def set_project_status(self, user: User, project_id: UUID, status: ProjectStatus) -> Project:
        """Пауза или возобновление автоматической работы проекта."""
        project = await self.get_project(user, project_id)
        if project.status == status:
            return project
        previous = project.status
        project.status = status
        await self._session.flush()
        await self._audit.record(
            action="project.status_changed",
            actor_type=ActorType.USER,
            actor_id=user.id,
            project_id=project.id,
            entity_type="project",
            entity_id=project.id,
            data={"from": previous, "to": status},
        )
        return project


def _ensure_timezone(value: str) -> None:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise DomainValidationError(f"Неизвестный часовой пояс: {value}") from exc
