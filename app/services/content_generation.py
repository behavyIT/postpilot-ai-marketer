"""Генерация стратегии, контент-плана и текстов постов (выполняется в воркере)."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.content_planner import ContentPlanner
from app.agents.copywriter import Copywriter, NumberedFact, PostBrief
from app.agents.strategist import Strategist
from app.ai.llm.base import LLMError
from app.ai.llm.service import CallContext, LLMService
from app.core.errors import AppError
from app.core.logging import get_logger
from app.db.models import (
    ContentPlan,
    MarketingStrategy,
    ProfileFact,
    Project,
    Publication,
    PublicationVariant,
    PublicationVersion,
)
from app.domain.enums import (
    ActorType,
    ContentGoal,
    DocumentStatus,
    FactStatus,
    Platform,
    ProfileField,
    PublicationStatus,
)
from app.domain.platforms import SPECS
from app.schemas.business_profile import BusinessProfileData
from app.schemas.content import StrategyData
from app.services.content_repo import ContentRepository
from app.services.memory import MemoryService
from app.services.profile_analysis import load_latest_profile

log = get_logger(__name__)

MAX_FACTS_FOR_POST = 40
_FIELD_PRIORITY = [
    ProfileField.PRODUCTS_SERVICES,
    ProfileField.PRICING,
    ProfileField.UNIQUE_SELLING_POINTS,
    ProfileField.COMPETITIVE_ADVANTAGES,
    ProfileField.PRODUCT_BENEFITS,
    ProfileField.GEOGRAPHY,
    ProfileField.CONTACTS,
    ProfileField.COMPANY_NAME,
    ProfileField.TARGET_AUDIENCE,
    ProfileField.CUSTOMER_PAINS,
    ProfileField.FAQ,
    ProfileField.COMMUNICATION_STYLE,
    ProfileField.INDUSTRY,
    ProfileField.ADVERTISING_RESTRICTIONS,
]


class ProfileMissingError(AppError):
    """Без профиля бизнеса нельзя строить стратегию и писать посты."""


@dataclass(slots=True)
class PlanOutcome:
    plan: ContentPlan
    items: list[Publication]
    notes: list[str]
    strategy_created: bool
    timezone: str


@dataclass(slots=True)
class DraftOutcome:
    publication: Publication
    text: str | None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


class ContentGenerationService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        llm: LLMService,
        platforms: list[Platform],
    ) -> None:
        self._sf = session_factory
        self._platforms = platforms
        self._strategist = Strategist(llm)
        self._planner = ContentPlanner(llm)
        self._copywriter = Copywriter(llm)

    # --- Стратегия ----------------------------------------------------------------------------

    async def generate_strategy(
        self, project_id: UUID, user_id: UUID, *, instructions: str | None = None
    ) -> tuple[MarketingStrategy, StrategyData]:
        async with self._sf() as session:
            profile = await _require_profile(session, project_id)
            memory = MemoryService(session)
            if instructions:
                await memory.add_preference(project_id, instructions, source="strategy")
            preferences = await memory.preferences(project_id)
            previous = await ContentRepository(session).latest_strategy(project_id)
            await session.commit()

        data = await self._strategist.create(
            profile,
            platforms=self._platforms,
            preferences=preferences,
            previous=StrategyData.model_validate(previous.data) if previous and instructions else None,
            instructions=instructions,
            ctx=CallContext(purpose="content.strategy", project_id=project_id, user_id=user_id),
        )
        async with self._sf() as session:
            version = (
                await session.scalar(
                    select(func.max(MarketingStrategy.version_no)).where(
                        MarketingStrategy.project_id == project_id
                    )
                )
                or 0
            )
            # Прежние черновики больше не нужны: в работе один черновик и, возможно, утверждённая стратегия.
            await session.execute(
                update(MarketingStrategy)
                .where(
                    MarketingStrategy.project_id == project_id,
                    MarketingStrategy.status == DocumentStatus.DRAFT,
                )
                .values(status=DocumentStatus.ARCHIVED)
            )
            strategy = MarketingStrategy(
                project_id=project_id,
                version_no=version + 1,
                status=DocumentStatus.DRAFT,
                data=data.model_dump(mode="json"),
                instructions=instructions,
            )
            session.add(strategy)
            await session.commit()
        return strategy, data

    # --- Контент-план -------------------------------------------------------------------------

    async def generate_plan(
        self, project_id: UUID, user_id: UUID, *, days: int, instructions: str | None = None
    ) -> PlanOutcome:
        days = max(1, min(days, 90))
        async with self._sf() as session:
            profile = await _require_profile(session, project_id)
            project = await session.get(Project, project_id)
            if project is None:
                raise AppError("Проект не найден")
            timezone = project.timezone
            existing = await ContentRepository(session).latest_strategy(project_id)

        strategy_created = False
        if existing is None:
            strategy_row, strategy = await self.generate_strategy(project_id, user_id)
            strategy_created = True
        else:
            strategy_row, strategy = existing, StrategyData.model_validate(existing.data)

        async with self._sf() as session:
            memory = MemoryService(session)
            if instructions:
                await memory.add_preference(project_id, instructions, source="plan")
            preferences = await memory.preferences(project_id)
            recent = await _recent_topics(session, project_id)
            await session.commit()

        start = datetime.now(ZoneInfo(timezone)).date() + timedelta(days=1)
        result = await self._planner.create(
            profile=profile,
            strategy=strategy,
            start=start,
            days=days,
            timezone=timezone,
            recent_topics=recent,
            preferences=preferences,
            instructions=instructions,
            ctx=CallContext(purpose="content.plan", project_id=project_id, user_id=user_id),
        )
        if not result.items:
            raise AppError(
                "Не получилось составить план: модель не предложила подходящих тем. Попробуйте ещё раз"
            )

        async with self._sf() as session:
            await session.execute(
                update(ContentPlan)
                .where(ContentPlan.project_id == project_id, ContentPlan.status == DocumentStatus.DRAFT)
                .values(status=DocumentStatus.ARCHIVED)
            )
            plan = ContentPlan(
                project_id=project_id,
                strategy_id=strategy_row.id,
                period_start=start,
                period_end=start + timedelta(days=days - 1),
                status=DocumentStatus.DRAFT,
                instructions=instructions,
                notes=result.notes,
                created_by_type=ActorType.USER,
            )
            session.add(plan)
            await session.flush()
            items = [
                Publication(
                    project_id=project_id,
                    content_plan_id=plan.id,
                    planned_at=item.planned_at,
                    category=item.category,
                    goal=item.goal,
                    topic=item.topic,
                    title=item.title,
                    brief=item.brief,
                    cta=item.cta,
                    hashtags=item.hashtags,
                    visual_idea=item.visual_idea,
                )
                for item in result.items
            ]
            session.add_all(items)
            await session.commit()
        return PlanOutcome(
            plan=plan, items=items, notes=result.notes, strategy_created=strategy_created, timezone=timezone
        )

    # --- Тексты постов ------------------------------------------------------------------------

    async def write_plan_posts(
        self, project_id: UUID, user_id: UUID, plan_id: UUID
    ) -> AsyncIterator[DraftOutcome]:
        """Пишет тексты для пунктов плана без текста; отдаёт результат по мере готовности."""
        async with self._sf() as session:
            await _require_profile(session, project_id)
            repo = ContentRepository(session)
            view = await repo.plan_view(await repo.get_plan(project_id, plan_id))
            pending = [p for p in view.items if p.id not in view.written]
        for publication in pending:
            outcome = await self._write(publication, user_id, instructions=None)
            yield outcome
            if outcome.error:
                return  # модель недоступна — не тратим попытки на остальные посты

    async def write_single_post(
        self, project_id: UUID, user_id: UUID, *, topic: str, instructions: str | None
    ) -> DraftOutcome:
        async with self._sf() as session:
            await _require_profile(session, project_id)
            if instructions:
                await MemoryService(session).add_preference(project_id, instructions, source="post")
            publication = Publication(
                project_id=project_id,
                content_plan_id=None,
                planned_at=None,
                category="Разовый пост",
                goal=ContentGoal.ENGAGEMENT,
                topic=topic[:300],
                title=topic[:300],
                brief=(instructions or topic)[:1000],
                cta="",
                hashtags=[],
                visual_idea="",
            )
            session.add(publication)
            await session.commit()
        return await self._write(publication, user_id, instructions=instructions)

    async def _write(
        self, publication: Publication, user_id: UUID, *, instructions: str | None
    ) -> DraftOutcome:
        project_id = publication.project_id
        async with self._sf() as session:
            facts = list(
                await session.scalars(select(ProfileFact).where(ProfileFact.project_id == project_id))
            )
            preferences = await MemoryService(session).preferences(project_id)
            strategy_row = await ContentRepository(session).latest_strategy(project_id)
        strategy = StrategyData.model_validate(strategy_row.data) if strategy_row else None
        selected = _select_facts(facts)
        numbered = {i: f for i, f in enumerate(selected, start=1)}
        spec = SPECS[Platform.TELEGRAM]

        try:
            post = await self._copywriter.write(
                PostBrief(
                    category=publication.category,
                    goal=publication.goal,
                    topic=publication.topic,
                    title=publication.title,
                    brief=publication.brief,
                    cta=publication.cta or "",
                    hashtags=list(publication.hashtags),
                    visual_idea=publication.visual_idea or "",
                ),
                spec=spec,
                strategy=strategy,
                facts=[NumberedFact(i, f.value) for i, f in numbered.items()],
                reference_text="\n".join(f"{f.value} {f.quote}" for f in facts),
                preferences=preferences,
                instructions=instructions,
                ctx=CallContext(purpose="content.post", project_id=project_id, user_id=user_id),
            )
        except LLMError as exc:
            return DraftOutcome(publication=publication, text=None, error=str(exc))

        async with self._sf() as session:
            variant = PublicationVariant(
                publication_id=publication.id,
                platform=Platform.TELEGRAM,
                status=PublicationStatus.DRAFT,
                current_version_no=1,
            )
            session.add(variant)
            await session.flush()
            session.add(
                PublicationVersion(
                    variant_id=variant.id,
                    version_no=1,
                    body=post.text,
                    hashtags=post.hashtags,
                    content_hash=hashlib.sha256(post.text.encode()).hexdigest(),
                    created_by_type=ActorType.AI,
                    change_request=instructions,
                    used_fact_ids=[str(numbered[n].id) for n in post.used_fact_numbers],
                    warnings=post.warnings,
                )
            )
            await session.commit()
        return DraftOutcome(publication=publication, text=post.text, warnings=post.warnings)


async def _require_profile(session: AsyncSession, project_id: UUID) -> BusinessProfileData:
    latest = await load_latest_profile(session, project_id)
    if latest is None:
        raise ProfileMissingError(
            "Сначала мне нужно узнать бизнес: расскажите о нём или пришлите сайт и документы."
        )
    return BusinessProfileData.model_validate(latest.data)


async def _recent_topics(session: AsyncSession, project_id: UUID) -> list[str]:
    since = datetime.now(UTC) - timedelta(days=30)
    rows = await session.scalars(
        select(Publication.title)
        .outerjoin(ContentPlan, ContentPlan.id == Publication.content_plan_id)
        .where(
            Publication.project_id == project_id,
            or_(Publication.planned_at.is_(None), Publication.planned_at >= since),
            or_(ContentPlan.id.is_(None), ContentPlan.status != DocumentStatus.ARCHIVED),
        )
        .order_by(Publication.created_at.desc())
        .limit(40)
    )
    return list(rows)


def _select_facts(facts: list[ProfileFact]) -> list[ProfileFact]:
    """До 40 фактов: сначала со слов владельца, затем по важности пункта для текста."""
    order = {f: i for i, f in enumerate(_FIELD_PRIORITY)}
    ranked = sorted(
        facts,
        key=lambda f: (f.status != FactStatus.CONFIRMED, order.get(ProfileField(f.field), 99)),
    )
    return ranked[:MAX_FACTS_FOR_POST]


def local_date(value: datetime, timezone: str) -> date:
    return value.astimezone(ZoneInfo(timezone)).date()
