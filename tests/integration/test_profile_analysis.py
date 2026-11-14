"""Конвейер изучения бизнеса целиком: настоящая БД, файлы на диске, сайт и модель — подменённые."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.ai.llm.base import LLMRequest, LLMUnavailableError, ModelProfile
from app.ai.llm.fake import FakeLLMProvider
from app.ai.llm.limiter import NullLimiter
from app.ai.llm.service import LLMService
from app.ai.llm.usage import MemoryUsageSink
from app.db.models import (
    BusinessDocument,
    BusinessNote,
    BusinessProfile,
    BusinessProfileVersion,
    ProfileFact,
    User,
    Website,
    WebsitePage,
)
from app.db.session import create_session_factory
from app.domain.enums import FactStatus, ProfileField, SourceStatus
from app.integrations.storage.local import LocalFileStorage
from app.integrations.web.crawler import SiteCrawler
from app.integrations.web.fetcher import SafeFetcher
from app.services.access import AccessService, TelegramProfile
from app.services.profile_analysis import ProfileAnalysisService
from app.services.sources import SourceService
from tests.integration.helpers import real_docx_bytes

pytestmark = pytest.mark.integration

SITE = {
    "/robots.txt": httpx.Response(404),
    "/": httpx.Response(
        200,
        headers={"content-type": "text/html; charset=utf-8"},
        content="<html><head><title>Зерно</title></head><body><p>Кофейня «Зерно» в Казани.</p>"
        "<p>Капучино — 250 ₽</p></body></html>".encode(),
    ),
}


@pytest.fixture
async def sf(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Сервис фиксирует свои транзакции, поэтому здесь реальные commit; в конце всё удаляем."""
    factory = create_session_factory(engine)
    yield factory
    async with factory() as session:
        await session.execute(delete(User).where(User.telegram_user_id.between(8000, 8999)))
        await session.commit()


def crawler(site: dict[str, httpx.Response]) -> tuple[SiteCrawler, SafeFetcher]:
    fetcher = SafeFetcher(transport=httpx.MockTransport(lambda r: site.get(r.url.path, httpx.Response(404))))
    return SiteCrawler(fetcher, max_pages=5, delay_s=0), fetcher


def fake_model(*, fail_extraction: bool = False) -> FakeLLMProvider:
    def responder(request: LLMRequest) -> str | dict[str, Any] | Exception:
        if request.images:
            return "Меню на доске: Раф — 300 ₽"
        system = request.messages[0].content
        if "выпиши факты" in system:
            if fail_extraction:
                return LLMUnavailableError("GigaChat временно недоступен")
            return {
                "facts": [
                    {"field": "company_name", "value": "Кофейня «Зерно»", "quote": "Кофейня «Зерно»"},
                    {"field": "geography", "value": "Казань", "quote": "в Казани"},
                    {"field": "pricing", "value": "Капучино 250 ₽", "quote": "Капучино — 250 ₽"},
                    {"field": "pricing", "value": "Раф 300 ₽", "quote": "Раф — 300 ₽"},
                    {"field": "target_audience", "value": "Студенты", "quote": "гости — студенты"},
                    {"field": "competitive_advantages", "value": "Лучшие в мире", "quote": "лучшие в мире"},
                ],
                "suspicious_instructions": False,
            }
        # Синтез профиля: номера фактов указаны только для географии — остальное соберёт код.
        return {
            "summary": "Кофейня в Казани для студентов.",
            "fields": [
                {"field": "geography", "value": "Казань", "fact_ids": [2]},
                {"field": "industry", "value": "Ресторанный бизнес", "fact_ids": []},
            ],
            "questions": ["Есть ли доставка?"],
        }

    return FakeLLMProvider(responder=responder)


async def make_project(sf: async_sessionmaker[AsyncSession], tmp: Path, telegram_id: int):
    async with sf() as session:
        access = AccessService(session, default_timezone="Europe/Moscow")
        user, _ = await access.register_or_update_user(TelegramProfile(telegram_user_id=telegram_id))
        project = await access.create_project(user, "Зерно")
        sources = SourceService(session, LocalFileStorage(tmp), max_upload_bytes=10 * 1024 * 1024)
        await sources.add_note(user, project, "Наши гости — студенты")
        await sources.add_website(user, project, "https://zerno.example/")
        await sources.add_document(
            user, project, filename="Услуги.docx", data=real_docx_bytes("Доставка по городу")
        )
        await sources.add_document(user, project, filename="Меню.png", data=b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        await session.commit()
        return user, project


def service(sf, tmp: Path, provider: FakeLLMProvider, site_crawler: SiteCrawler) -> ProfileAnalysisService:
    llm = LLMService(
        provider,
        models={ModelProfile.SMART: "ultra", ModelProfile.FAST: "lite", ModelProfile.VISION: "max"},
        limiter=NullLimiter(),
        usage=MemoryUsageSink(),
    )
    return ProfileAnalysisService(sf, llm=llm, storage=LocalFileStorage(tmp), crawler=site_crawler)


async def test_full_pipeline_builds_grounded_profile(sf, tmp_path: Path) -> None:
    user, project = await make_project(sf, tmp_path, 8001)
    site_crawler, fetcher = crawler(SITE)
    provider = fake_model()

    report = await service(sf, tmp_path, provider, site_crawler).run(project.id, user_id=user.id)
    await fetcher.aclose()

    assert report.websites == [("https://zerno.example/", 1)]
    assert sorted(report.documents) == ["Меню.png", "Услуги.docx"]
    assert report.facts_added == 5
    assert report.facts_rejected == 1  # «лучшие в мире» — цитаты нет ни в одном источнике
    assert report.profile_version == 1
    assert report.questions == ["Есть ли доставка?"]
    assert report.has_pending_sources is False

    profile = report.profile
    assert profile is not None
    assert profile.get(ProfileField.GEOGRAPHY).value == "Казань"  # формулировка модели с номерами фактов
    assert profile.get(ProfileField.INDUSTRY).value is None  # без опоры на факты не принимаем
    assert profile.get(ProfileField.PRICING).value == "Капучино 250 ₽; Раф 300 ₽"  # собрано из фактов кодом
    assert profile.get(ProfileField.TARGET_AUDIENCE).status is FactStatus.CONFIRMED  # со слов владельца
    assert profile.get(ProfileField.PRICING).status is FactStatus.INFERRED

    async with sf() as session:
        site = await session.scalar(select(Website).where(Website.project_id == project.id))
        assert site is not None and site.status == SourceStatus.PROCESSED and site.pages_count == 1
        docs = {d.title: d for d in await session.scalars(select(BusinessDocument))}
        assert "Доставка по городу" in (docs["Услуги.docx"].extracted_text or "")
        assert docs["Меню.png"].extracted_text == "Меню на доске: Раф — 300 ₽"  # прочитано моделью
        assert (
            await session.scalar(
                select(func.count())
                .select_from(BusinessNote)
                .where(BusinessNote.project_id == project.id, BusinessNote.analyzed_at.is_(None))
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(WebsitePage)
                .where(WebsitePage.project_id == project.id, WebsitePage.analyzed_at.is_(None))
            )
            == 0
        )
        stored = await session.scalar(select(BusinessProfile).where(BusinessProfile.project_id == project.id))
        assert stored is not None and stored.completeness == round(4 * 100 / 14)  # 5 фактов в 4 пунктах


async def test_second_run_is_incremental_and_force_makes_new_version(sf, tmp_path: Path) -> None:
    user, project = await make_project(sf, tmp_path, 8002)
    site_crawler, fetcher = crawler(SITE)
    provider = fake_model()
    svc = service(sf, tmp_path, provider, site_crawler)
    await svc.run(project.id, user_id=user.id)
    calls_after_first = len(provider.requests)

    again = await svc.run(project.id, user_id=user.id)
    assert len(provider.requests) == calls_after_first  # новых источников нет — модель не вызывалась
    assert again.profile_version == 1 and again.profile is not None

    forced = await svc.run(project.id, user_id=user.id, force=True)
    assert forced.profile_version == 2
    await fetcher.aclose()

    async with sf() as session:
        versions = await session.scalar(
            select(func.count())
            .select_from(BusinessProfileVersion)
            .join(BusinessProfile)
            .where(BusinessProfile.project_id == project.id)
        )
        assert versions == 2
        assert (
            await session.scalar(
                select(func.count()).select_from(ProfileFact).where(ProfileFact.project_id == project.id)
            )
            == 5
        )  # факты не задвоились


async def test_failures_are_reported_and_sources_kept_for_retry(sf, tmp_path: Path) -> None:
    user, project = await make_project(sf, tmp_path, 8003)
    broken_site, fetcher = crawler({"/robots.txt": httpx.Response(404)})  # все страницы 404
    report = await service(sf, tmp_path, fake_model(fail_extraction=True), broken_site).run(
        project.id, user_id=user.id
    )
    await fetcher.aclose()

    assert any("Сайт https://zerno.example/" in f for f in report.failures)
    assert any("Не удалось разобрать" in f for f in report.failures)
    assert report.has_pending_sources is True
    async with sf() as session:
        site = await session.scalar(select(Website).where(Website.project_id == project.id))
        assert site is not None and site.status == SourceStatus.FAILED
        unanalyzed = await session.scalar(
            select(func.count())
            .select_from(BusinessNote)
            .where(BusinessNote.project_id == project.id, BusinessNote.analyzed_at.is_(None))
        )
        assert unanalyzed == 1  # будет разобрано при следующем запуске
