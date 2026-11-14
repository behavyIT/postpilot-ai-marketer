from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainValidationError, NotFoundError
from app.db.models import AuditLog, ProjectMember, User
from app.domain.enums import ProjectRole
from app.services.access import AccessService, TelegramProfile

pytestmark = pytest.mark.integration


def make_service(session: AsyncSession) -> AccessService:
    return AccessService(session, default_timezone="Europe/Moscow")


def profile(telegram_id: int, **kwargs: str) -> TelegramProfile:
    return TelegramProfile(
        telegram_user_id=telegram_id,
        first_name=kwargs.get("first_name", "Анна"),
        username=kwargs.get("username"),
    )


async def test_registration_creates_user_once_and_updates_profile(session: AsyncSession) -> None:
    service = make_service(session)

    user, created = await service.register_or_update_user(profile(1001, username="anna"))
    assert created is True
    assert user.timezone == "Europe/Moscow"

    again, created_again = await service.register_or_update_user(profile(1001, username="anna_new"))
    assert created_again is False
    assert again.id == user.id
    assert again.username == "anna_new"

    count = len((await session.scalars(select(User).where(User.telegram_user_id == 1001))).all())
    assert count == 1
    actions = (await session.scalars(select(AuditLog.action).where(AuditLog.actor_id == user.id))).all()
    assert actions == ["user.registered"]


async def test_consent_is_recorded_once(session: AsyncSession) -> None:
    service = make_service(session)
    user, _ = await service.register_or_update_user(profile(1002))

    await service.accept_consent(user)
    first_consent = user.consent_at
    await service.accept_consent(user)

    assert first_consent is not None
    assert user.consent_at == first_consent


async def test_create_project_makes_owner_and_active(session: AsyncSession) -> None:
    service = make_service(session)
    user, _ = await service.register_or_update_user(profile(1003))

    project = await service.create_project(user, "  Кофейня   «Зерно»  ")

    assert project.name == "Кофейня «Зерно»"
    assert project.timezone == "Europe/Moscow"
    assert user.active_project_id == project.id
    member = await session.get(ProjectMember, {"project_id": project.id, "user_id": user.id})
    assert member is not None
    assert member.role == ProjectRole.OWNER
    audit = await session.scalar(
        select(AuditLog).where(AuditLog.action == "project.created", AuditLog.project_id == project.id)
    )
    assert audit is not None


@pytest.mark.parametrize("bad_name", ["", "   ", "x" * 121])
async def test_create_project_validates_name(session: AsyncSession, bad_name: str) -> None:
    service = make_service(session)
    user, _ = await service.register_or_update_user(profile(1004))
    with pytest.raises(DomainValidationError):
        await service.create_project(user, bad_name)


async def test_create_project_rejects_unknown_timezone(session: AsyncSession) -> None:
    service = make_service(session)
    user, _ = await service.register_or_update_user(profile(1005))
    with pytest.raises(DomainValidationError):
        await service.create_project(user, "Пекарня", timezone="Nowhere/City")


async def test_projects_are_isolated_between_users(session: AsyncSession) -> None:
    service = make_service(session)
    alice, _ = await service.register_or_update_user(profile(2001, first_name="Алиса"))
    bob, _ = await service.register_or_update_user(profile(2002, first_name="Борис"))
    alice_project = await service.create_project(alice, "Цветочный магазин")
    bob_project = await service.create_project(bob, "Автосервис")

    assert [p.id for p in await service.list_projects(alice)] == [alice_project.id]
    assert [p.id for p in await service.list_projects(bob)] == [bob_project.id]

    with pytest.raises(NotFoundError):
        await service.get_project(bob, alice_project.id)
    with pytest.raises(NotFoundError):
        await service.set_active_project(bob, alice_project.id)
    assert bob.active_project_id == bob_project.id


async def test_missing_project_and_foreign_project_give_same_error(session: AsyncSession) -> None:
    service = make_service(session)
    user, _ = await service.register_or_update_user(profile(2003))
    with pytest.raises(NotFoundError) as missing:
        await service.get_project(user, uuid4())
    assert str(missing.value) == "Проект не найден"


async def test_list_projects_newest_first_and_switch_active(session: AsyncSession) -> None:
    service = make_service(session)
    user, _ = await service.register_or_update_user(profile(2004))
    first = await service.create_project(user, "Первый")
    second = await service.create_project(user, "Второй")

    assert [p.id for p in await service.list_projects(user)] == [second.id, first.id]
    assert user.active_project_id == second.id

    await service.set_active_project(user, first.id)
    active = await service.get_active_project(user)
    assert active is not None
    assert active.id == first.id
