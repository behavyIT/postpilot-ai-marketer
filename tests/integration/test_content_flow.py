"""Стратегия и план из чата: предусловия, кнопки утверждения, постановка задач и фоновая задача."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.orchestrator.intents import Intent
from app.agents.orchestrator.replies import Action
from app.ai.llm.fake import FakeLLMProvider
from app.bot.notifier import MemoryNotifier
from app.core.config import Settings
from app.db.models import (
    BusinessProfile,
    BusinessProfileVersion,
    ContentPlan,
    MarketingStrategy,
    Project,
    Publication,
)
from app.domain.enums import DocumentStatus
from app.schemas.business_profile import BusinessProfileData
from app.services.content_generation import DraftOutcome, PlanOutcome
from app.services.jobs import ContentJob, ContentJobKind, MemoryJobQueue, ProfileAnalysisJob
from app.workers.tasks.content import content_queued_key, run_content_job
from tests.integration.helpers import intent, make_orchestrator, make_user, turn_for
from tests.unit.test_strategy_and_plan import strategy

pytestmark = pytest.mark.integration


async def with_project(session: AsyncSession, settings: Settings, telegram_id: int, *, profile: bool = True):
    user = await make_user(session, telegram_id)
    jobs = MemoryJobQueue()
    orchestrator = make_orchestrator(session, settings, FakeLLMProvider(), jobs)
    turn = await turn_for(session, user)
    await orchestrator.create_project(turn, "Зерно")
    assert turn.project is not None
    if profile:
        row = BusinessProfile(project_id=turn.project.id, latest_version_no=1, completeness=0)
        session.add(row)
        await session.flush()
        session.add(
            BusinessProfileVersion(
                profile_id=row.id, version_no=1, data=BusinessProfileData().model_dump(mode="json")
            )
        )
        await session.flush()
    return user, turn, orchestrator, jobs


async def test_content_requests_need_a_profile(session: AsyncSession, test_settings: Settings) -> None:
    _, turn, orchestrator, jobs = await with_project(session, test_settings, 9101, profile=False)

    reply = await orchestrator.handle_command(turn, Intent.CREATE_STRATEGY, "/strategy")
    assert "нужно узнать бизнес" in reply.text
    assert jobs.jobs == []

    await orchestrator.handle_text(turn, "coffee.ru", urls=["coffee.ru"])  # появился источник
    jobs.jobs.clear()
    reply = await orchestrator.handle_command(turn, Intent.CREATE_PLAN, "/plan")
    assert "Сначала изучу присланные материалы" in reply.text
    assert isinstance(jobs.jobs[0], ProfileAnalysisJob)


async def test_strategy_plan_and_post_requests_enqueue_jobs(
    session: AsyncSession, test_settings: Settings
) -> None:
    user, turn, _, jobs = await with_project(session, test_settings, 9102)
    provider = FakeLLMProvider(
        intent("create_strategy", instructions="упор на студентов"),
        intent("create_plan", period_days=14),
        intent("create_post", topic="осенний раф"),
    )
    orchestrator = make_orchestrator(session, test_settings, provider, jobs)

    assert (
        "Готовлю стратегию" in (await orchestrator.handle_text(turn, "Стратегию с упором на студентов")).text
    )
    assert "на две недели" in (await orchestrator.handle_text(turn, "План на две недели")).text
    assert "Пишу пост" in (await orchestrator.handle_text(turn, "Пост про осенний раф")).text

    kinds = [(j.kind, j.days, j.instructions, j.topic) for j in jobs.content_jobs]
    assert kinds == [
        (ContentJobKind.STRATEGY, None, "упор на студентов", None),
        (ContentJobKind.PLAN, 14, None, None),
        (ContentJobKind.SINGLE_POST, None, None, "осенний раф"),
    ]
    assert all(j.user_id == user.id and j.chat_id == 777 for j in jobs.content_jobs)


async def test_existing_strategy_is_shown_and_approved_by_button(
    session: AsyncSession, test_settings: Settings
) -> None:
    _, turn, orchestrator, jobs = await with_project(session, test_settings, 9103)
    assert turn.project is not None
    old = MarketingStrategy(
        project_id=turn.project.id, version_no=1, status=DocumentStatus.APPROVED, data=strategy().model_dump()
    )
    new = MarketingStrategy(
        project_id=turn.project.id, version_no=2, status=DocumentStatus.DRAFT, data=strategy().model_dump()
    )
    session.add_all([old, new])
    await session.flush()

    shown = await orchestrator.handle_command(turn, Intent.CREATE_STRATEGY, "/strategy")
    assert "вариант 1 · утверждена" in shown.text  # утверждённая важнее черновика
    assert jobs.jobs == []

    approved = await orchestrator.handle_button(turn, Action.STRATEGY_APPROVE, str(new.id))
    assert "Стратегия утверждена (вариант 2)" in approved.text
    await session.refresh(old)
    assert old.status == DocumentStatus.ARCHIVED
    assert new.status == DocumentStatus.APPROVED

    stale = await orchestrator.handle_button(turn, Action.STRATEGY_APPROVE, str(old.id))
    assert "устаревший вариант" in stale.text


async def test_plan_buttons_approve_write_and_redo(session: AsyncSession, test_settings: Settings) -> None:
    _, turn, orchestrator, jobs = await with_project(session, test_settings, 9104)
    assert turn.project is not None
    plan = ContentPlan(
        project_id=turn.project.id, period_start=date(2026, 11, 17), period_end=date(2026, 11, 30)
    )
    session.add(plan)
    await session.flush()
    session.add(
        Publication(
            project_id=turn.project.id,
            content_plan_id=plan.id,
            planned_at=datetime(2026, 11, 17, 6, tzinfo=UTC),
            category="Полезное",
            goal="education",
            topic="Зерно",
            title="Как выбрать зерно",
            brief="",
            hashtags=[],
        )
    )
    await session.flush()

    shown = await orchestrator.handle_command(turn, Intent.SHOW_SCHEDULE, "/schedule")
    assert "«Как выбрать зерно»" in shown.text

    approved = await orchestrator.handle_button(turn, Action.PLAN_APPROVE, str(plan.id))
    assert "План утверждён: публикаций — 1" in approved.text
    assert plan.status == DocumentStatus.APPROVED

    writing = await orchestrator.handle_button(turn, Action.PLAN_WRITE, str(plan.id))
    assert "постов — 1" in writing.text
    redo = await orchestrator.handle_button(turn, Action.PLAN_REDO, str(plan.id))
    assert "две недели" in redo.text  # период пересоставляемого плана — 14 дней
    assert [(j.kind, j.plan_id, j.days) for j in jobs.content_jobs] == [
        (ContentJobKind.PLAN_POSTS, plan.id, None),
        (ContentJobKind.PLAN, None, 14),
    ]

    foreign = await orchestrator.handle_button(turn, Action.PLAN_APPROVE, str(uuid4()))
    assert "не найден" in foreign.text


async def test_free_text_wish_is_remembered(session: AsyncSession, test_settings: Settings) -> None:
    _, turn, _, jobs = await with_project(session, test_settings, 9106)
    assert turn.project is not None
    session.add(
        MarketingStrategy(
            project_id=turn.project.id, version_no=1, status="draft", data=strategy().model_dump()
        )
    )
    await session.flush()
    provider = FakeLLMProvider(
        intent("add_preference", instructions="публиковать посты чаще"),
        intent("add_preference", instructions="Публиковать  посты чаще"),
    )
    orchestrator = make_orchestrator(session, test_settings, provider, jobs)

    first = await orchestrator.handle_text(turn, "пожелание, хочу делать часто посты")
    again = await orchestrator.handle_text(turn, "хочу посты почаще")

    assert first.text.startswith("Запомнил: «публиковать посты чаще»")
    assert first.buttons[0][0].action is Action.STRATEGY_REDO
    assert again.text.startswith("Это я уже помню")
    from app.services.memory import MemoryService

    assert await MemoryService(session).preferences(turn.project.id) == ["публиковать посты чаще"]
    settings_reply = await orchestrator.handle_command(turn, Intent.SETTINGS, "/settings")
    assert "• публиковать посты чаще" in settings_reply.text
    assert "запомни: пиши без эмодзи" in settings_reply.text  # подсказка, как добавить пожелание


async def test_preferences_are_shown_and_forgotten(session: AsyncSession, test_settings: Settings) -> None:
    _, turn, orchestrator, _ = await with_project(session, test_settings, 9105)
    assert turn.project is not None
    from app.services.memory import MemoryService

    memory = MemoryService(session)
    await memory.add_preference(turn.project.id, "без эмодзи", source="post")
    await memory.add_preference(turn.project.id, "Без   эмодзи", source="post")  # дубль не сохраняется

    settings_reply = await orchestrator.handle_command(turn, Intent.SETTINGS, "/settings")
    assert "• без эмодзи" in settings_reply.text
    assert settings_reply.buttons[0][0].action is Action.MEMORY_FORGET

    forgot = await orchestrator.handle_button(turn, Action.MEMORY_FORGET, "")
    assert "Забыл все ваши пожелания" in forgot.text
    assert await memory.preferences(turn.project.id) == []


@dataclass
class FakeContentService:
    error: Exception | None = None

    async def generate_strategy(self, project_id, user_id, *, instructions=None):
        if self.error:
            raise self.error
        row = MarketingStrategy(id=uuid4(), project_id=project_id, version_no=3, status="draft", data={})
        return row, strategy()

    async def write_plan_posts(self, project_id, user_id, plan_id):
        for title in ("Первый", "Второй"):
            pub = Publication(
                project_id=project_id,
                category="Полезное",
                goal="education",
                topic=title,
                title=title,
                brief="",
            )
            yield DraftOutcome(publication=pub, text=f"<b>{title}</b>", warnings=[])

    async def generate_plan(self, *args, **kwargs) -> PlanOutcome:  # pragma: no cover — не используется
        raise NotImplementedError


@dataclass
class FakeDeps:
    redis: Redis
    session_factory: object
    content_service: FakeContentService
    notifier: MemoryNotifier


async def run_job(session_factory, redis: Redis, service: FakeContentService, project: Project, kind, **kw):
    deps = FakeDeps(
        redis=redis, session_factory=session_factory, content_service=service, notifier=MemoryNotifier()
    )
    job = ContentJob(kind=kind, project_id=project.id, user_id=uuid4(), chat_id=42, **kw)
    await redis.set(content_queued_key(project.id, kind), "1")
    await run_content_job(deps, job)  # type: ignore[arg-type]
    assert await redis.get(content_queued_key(project.id, kind)) is None
    return deps.notifier.sent


async def test_worker_sends_strategy_posts_and_errors(engine, redis_client: Redis) -> None:
    from sqlalchemy import delete

    from app.db.session import create_session_factory
    from app.services.access import AccessService, TelegramProfile

    factory = create_session_factory(engine)
    async with factory() as session:
        access = AccessService(session, default_timezone="Europe/Moscow")
        user, _ = await access.register_or_update_user(TelegramProfile(telegram_user_id=8199))
        project = await access.create_project(user, "Зерно")
        await session.commit()
    try:
        sent = await run_job(factory, redis_client, FakeContentService(), project, ContentJobKind.STRATEGY)
        assert "Стратегия продвижения «Зерно»" in sent[0][1].text and sent[0][0] == 42

        sent = await run_job(
            factory, redis_client, FakeContentService(), project, ContentJobKind.PLAN_POSTS, plan_id=uuid4()
        )
        assert all(r.text.startswith("<b>Черновик поста</b> · Полезное") for _, r in sent[:2])
        assert "<b>Первый</b>" in sent[0][1].text
        assert "написал черновиков — 2" in sent[-1][1].text

        sent = await run_job(
            factory,
            redis_client,
            FakeContentService(error=RuntimeError("boom")),
            project,
            ContentJobKind.STRATEGY,
        )
        assert "Что-то пошло не так" in sent[0][1].text
    finally:
        async with factory() as session:
            from app.db.models import User

            await session.execute(delete(User).where(User.telegram_user_id == 8199))
            await session.commit()
