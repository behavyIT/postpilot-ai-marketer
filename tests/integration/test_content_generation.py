"""Генерация стратегии, плана и постов: настоящая БД, модель подменена."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.ai.llm.base import LLMRequest, LLMUnavailableError, ModelProfile
from app.ai.llm.fake import FakeLLMProvider
from app.ai.llm.limiter import NullLimiter
from app.ai.llm.service import LLMService
from app.ai.llm.usage import MemoryUsageSink
from app.db.models import (
    AgentMemory,
    BusinessProfile,
    BusinessProfileVersion,
    ContentPlan,
    MarketingStrategy,
    ProfileFact,
    Publication,
    PublicationVariant,
    PublicationVersion,
    User,
)
from app.db.session import create_session_factory
from app.domain.enums import DocumentStatus, FactStatus, Platform, ProfileField, PublicationStatus, SourceKind
from app.schemas.business_profile import BusinessProfileData, ProfileFieldValue
from app.services.access import AccessService, TelegramProfile
from app.services.content_generation import ContentGenerationService, ProfileMissingError
from tests.unit.test_strategy_and_plan import strategy

pytestmark = pytest.mark.integration


@pytest.fixture
async def sf(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = create_session_factory(engine)
    yield factory
    async with factory() as session:
        await session.execute(delete(User).where(User.telegram_user_id.between(8100, 8199)))
        await session.commit()


async def make_project(sf: async_sessionmaker[AsyncSession], telegram_id: int, *, with_profile: bool = True):
    async with sf() as session:
        access = AccessService(session, default_timezone="Europe/Moscow")
        user, _ = await access.register_or_update_user(TelegramProfile(telegram_user_id=telegram_id))
        project = await access.create_project(user, "Зерно")
        if with_profile:
            data = BusinessProfileData(summary="Кофейня у КФУ")
            data.fields[ProfileField.PRICING] = ProfileFieldValue(
                value="Средний чек 350 ₽", status=FactStatus.CONFIRMED
            )
            profile = BusinessProfile(project_id=project.id, latest_version_no=1, completeness=7)
            session.add(profile)
            await session.flush()
            session.add(
                BusinessProfileVersion(profile_id=profile.id, version_no=1, data=data.model_dump(mode="json"))
            )
            session.add(
                ProfileFact(
                    project_id=project.id,
                    field=ProfileField.PRICING,
                    value="Средний чек 350 ₽",
                    quote="средний чек 350 ₽",
                    source_kind=SourceKind.NOTE,
                    source_id=project.id,
                    source_label="Сообщение владельца",
                    status=FactStatus.CONFIRMED,
                )
            )
        await session.commit()
        return user, project


def model(*, fail_posts: bool = False) -> FakeLLMProvider:
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()

    def responder(request: LLMRequest) -> str | dict[str, Any] | Exception:
        system = request.messages[0].content
        if "маркетолог-стратег" in system:
            return strategy().model_dump()
        if "контент-менеджер" in system:
            return {
                "items": [
                    {
                        "date": (tomorrow + timedelta(days=i)).isoformat(),
                        "time": "09:00",
                        "platform": "telegram",
                        "rubric": "Полезное",
                        "goal": "education",
                        "topic": topic,
                        "title": topic,
                        "brief": "О чём пост",
                        "cta": "Заходи",
                        "hashtags": ["кофе"],
                        "visual_idea": "чашка",
                    }
                    for i, topic in enumerate(
                        ["Как выбрать зерно", "Осенний раф с корицей", "Наша команда бариста"]
                    )
                ]
            }
        if fail_posts:
            return LLMUnavailableError("GigaChat временно недоступен")
        return {
            "body": "<b>Пост</b>\nСредний чек 350 ₽, скидка 15%",
            "hashtags": ["кофе"],
            "used_fact_ids": [1],
        }

    return FakeLLMProvider(responder=responder)


def service(sf: async_sessionmaker[AsyncSession], provider: FakeLLMProvider) -> ContentGenerationService:
    llm = LLMService(
        provider,
        models={ModelProfile.SMART: "ultra", ModelProfile.FAST: "lite"},
        limiter=NullLimiter(),
        usage=MemoryUsageSink(),
    )
    return ContentGenerationService(sf, llm=llm, platforms=[Platform.TELEGRAM])


async def test_strategy_versions_and_preferences(sf) -> None:
    user, project = await make_project(sf, 8101)
    svc = service(sf, model())

    first, _ = await svc.generate_strategy(project.id, user.id)
    second, data = await svc.generate_strategy(project.id, user.id, instructions="упор на студентов")

    assert (first.version_no, second.version_no) == (1, 2)
    assert sum(r.share_percent for r in data.rubrics) == 100
    async with sf() as session:
        statuses = dict(
            (
                await session.execute(
                    select(MarketingStrategy.version_no, MarketingStrategy.status).where(
                        MarketingStrategy.project_id == project.id
                    )
                )
            ).all()
        )
        assert statuses == {1: DocumentStatus.ARCHIVED, 2: DocumentStatus.DRAFT}
        memory = await session.scalars(
            select(AgentMemory.content).where(AgentMemory.project_id == project.id)
        )
        assert list(memory) == ["упор на студентов"]


async def test_plan_creates_strategy_when_missing_and_stores_items(sf) -> None:
    user, project = await make_project(sf, 8102)
    outcome = await service(sf, model()).generate_plan(project.id, user.id, days=7)

    assert outcome.strategy_created is True
    assert [p.title for p in outcome.items] == [
        "Как выбрать зерно",
        "Осенний раф с корицей",
        "Наша команда бариста",
    ]
    assert outcome.items[0].planned_at is not None
    assert outcome.items[0].planned_at.hour == 6  # 09:00 по Москве в UTC
    assert outcome.plan.status == DocumentStatus.DRAFT
    assert (outcome.plan.period_end - outcome.plan.period_start).days == 6


async def test_new_plan_archives_old_draft_and_avoids_repeating_topics(sf) -> None:
    user, project = await make_project(sf, 8103)
    svc = service(sf, model())
    first = await svc.generate_plan(project.id, user.id, days=7)

    with pytest.raises(Exception, match="не предложила подходящих тем"):
        await svc.generate_plan(project.id, user.id, days=7)  # модель повторяет те же темы — всё отсеяно

    async with sf() as session:
        plan = await session.get(ContentPlan, first.plan.id)
        assert (
            plan is not None and plan.status == DocumentStatus.DRAFT
        )  # неудачная попытка ничего не испортила


async def test_drafts_are_written_once_with_warnings(sf) -> None:
    user, project = await make_project(sf, 8104)
    svc = service(sf, model())
    outcome = await svc.generate_plan(project.id, user.id, days=7)

    drafts = [d async for d in svc.write_plan_posts(project.id, user.id, outcome.plan.id)]
    again = [d async for d in svc.write_plan_posts(project.id, user.id, outcome.plan.id)]

    assert len(drafts) == 3 and again == []
    assert drafts[0].text == "<b>Пост</b>\nСредний чек 350 ₽, скидка 15%\n\n#кофе"
    assert any("«15%»" in w for w in drafts[0].warnings)  # скидки нет в фактах
    async with sf() as session:
        versions = list(
            await session.scalars(
                select(PublicationVersion)
                .join(PublicationVariant)
                .join(Publication)
                .where(Publication.project_id == project.id)
            )
        )
        assert len(versions) == 3
        assert all(v.version_no == 1 and len(v.content_hash) == 64 for v in versions)
        assert all(len(v.used_fact_ids) == 1 for v in versions)
        variants = await session.scalars(
            select(PublicationVariant.status).join(Publication).where(Publication.project_id == project.id)
        )
        assert set(variants) == {PublicationStatus.DRAFT}


async def test_single_post_and_model_failure(sf) -> None:
    user, project = await make_project(sf, 8105)
    ok = await service(sf, model()).write_single_post(
        project.id, user.id, topic="Осенний раф", instructions="без эмодзи"
    )
    failed = await service(sf, model(fail_posts=True)).write_single_post(
        project.id, user.id, topic="Какао", instructions=None
    )

    assert ok.text is not None and ok.error is None
    assert failed.text is None and failed.error == "GigaChat временно недоступен"
    async with sf() as session:
        written = await session.scalar(
            select(func.count())
            .select_from(PublicationVersion)
            .join(PublicationVariant)
            .join(Publication)
            .where(Publication.project_id == project.id)
        )
        assert written == 1


async def test_profile_is_required(sf) -> None:
    user, project = await make_project(sf, 8106, with_profile=False)
    with pytest.raises(ProfileMissingError):
        await service(sf, model()).generate_strategy(project.id, user.id)
    assert isinstance(project.id, UUID)
