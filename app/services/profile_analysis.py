"""Конвейер изучения бизнеса: источники → текст → факты → профиль.

Запускается в воркере. Каждый шаг фиксирует результат в своей транзакции, поэтому
сбой посреди работы не теряет уже сделанное, а повторный запуск продолжит с места
остановки: обрабатываются только новые источники.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import unquote
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.business_analyst import (
    MAX_FACTS_PER_FIELD,
    BusinessAnalyst,
    KnownFact,
    ProfileSynthesis,
    SourceSegment,
    make_chunks,
    normalize_for_match,
)
from app.ai.llm.base import ImageInput, LLMError
from app.ai.llm.service import CallContext, LLMService
from app.ai.prompts import load_prompt
from app.core.errors import AppError
from app.core.logging import get_logger
from app.db.models import (
    BusinessDocument,
    BusinessNote,
    BusinessProfile,
    BusinessProfileVersion,
    ProfileFact,
    Project,
    StoredFile,
    Website,
    WebsitePage,
)
from app.domain.enums import ActorType, DocumentType, FactStatus, ProfileField, SourceKind, SourceStatus
from app.integrations.documents.extract import extract_text
from app.integrations.storage.base import FileStorage
from app.integrations.web.crawler import SiteCrawler
from app.integrations.web.html import structured_to_text
from app.schemas.business_profile import (
    FIELD_LABELS,
    KEY_FIELDS,
    BusinessProfileData,
    ProfileFieldValue,
    SourceRef,
)

log = get_logger(__name__)

FALLBACK_QUESTIONS: dict[ProfileField, str] = {
    ProfileField.COMPANY_NAME: "Как называется ваш бизнес?",
    ProfileField.PRODUCTS_SERVICES: "Какие продукты или услуги вы продаёте? Назовите 3–5 главных.",
    ProfileField.TARGET_AUDIENCE: "Кто ваши основные клиенты: возраст, занятия, что для них важно?",
    ProfileField.GEOGRAPHY: "В каком городе или районе вы работаете? Есть ли доставка?",
    ProfileField.COMPETITIVE_ADVANTAGES: "Почему клиенты выбирают вас, а не конкурентов?",
}


@dataclass(slots=True)
class AnalysisReport:
    project_id: UUID
    project_name: str = ""
    websites: list[tuple[str, int]] = field(default_factory=list)  # (адрес, прочитано страниц)
    documents: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    facts_added: int = 0
    facts_rejected: int = 0
    suspicious_sources: list[str] = field(default_factory=list)
    profile: BusinessProfileData | None = None
    profile_version: int | None = None
    questions: list[str] = field(default_factory=list)
    has_pending_sources: bool = False


class ProfileAnalysisService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        llm: LLMService,
        storage: FileStorage,
        crawler: SiteCrawler,
    ) -> None:
        self._sf = session_factory
        self._llm = llm
        self._storage = storage
        self._crawler = crawler
        self._analyst = BusinessAnalyst(llm)

    async def run(self, project_id: UUID, *, user_id: UUID | None, force: bool = False) -> AnalysisReport:
        report = AnalysisReport(project_id=project_id)
        async with self._sf() as session:
            project = await session.get(Project, project_id)
            if project is None:
                raise AppError("Проект не найден")
            report.project_name = project.name

        await self._crawl_websites(project_id, report)
        await self._process_documents(project_id, user_id, report)
        extraction_complete = await self._extract_facts(project_id, user_id, report)

        if report.facts_added or force or not await self._has_profile(project_id):
            await self._synthesize(project_id, user_id, report)
        else:
            await self._load_latest(project_id, report)
        report.has_pending_sources = not extraction_complete or await self._has_unanalyzed(project_id)
        log.info(
            "profile.analysis_done",
            project_id=str(project_id),
            facts_added=report.facts_added,
            rejected=report.facts_rejected,
            version=report.profile_version,
            failures=len(report.failures),
        )
        return report

    # --- 1. Сайты -----------------------------------------------------------------------------

    async def _crawl_websites(self, project_id: UUID, report: AnalysisReport) -> None:
        async with self._sf() as session:
            sites = list(
                await session.scalars(
                    select(Website).where(
                        Website.project_id == project_id, Website.status == SourceStatus.PENDING
                    )
                )
            )
        for site in sites:
            try:
                result = await self._crawler.crawl(site.url)
                error = None if result.pages else _crawl_error(result.errors, result.skipped_by_robots)
            except AppError as exc:
                result, error = None, str(exc)

            async with self._sf() as session:
                stored = await session.get(Website, site.id)
                if stored is None:
                    continue
                stored.last_crawled_at = datetime.now(UTC)
                if result is not None and result.pages:
                    await session.execute(delete(WebsitePage).where(WebsitePage.website_id == stored.id))
                    for page in result.pages:
                        session.add(
                            WebsitePage(
                                website_id=stored.id,
                                project_id=project_id,
                                url=page.url,
                                title=(page.title or "")[:512] or None,
                                description=page.description,
                                http_status=page.http_status,
                                content_hash=page.content_hash,
                                text=page.text,
                                structured_data=page.structured_data,
                                fetched_at=page.fetched_at,
                            )
                        )
                    stored.pages_count = len(result.pages)
                    stored.status = SourceStatus.PROCESSED
                    stored.error = None
                    report.websites.append((stored.url, len(result.pages)))
                else:
                    stored.status = SourceStatus.FAILED
                    stored.error = error
                    report.failures.append(f"Сайт {stored.url}: {error}")
                await session.commit()

    # --- 2. Документы -------------------------------------------------------------------------

    async def _process_documents(
        self, project_id: UUID, user_id: UUID | None, report: AnalysisReport
    ) -> None:
        async with self._sf() as session:
            rows = (
                await session.execute(
                    select(BusinessDocument, StoredFile)
                    .join(StoredFile, StoredFile.id == BusinessDocument.file_id)
                    .where(
                        BusinessDocument.project_id == project_id,
                        BusinessDocument.status == SourceStatus.PENDING,
                    )
                )
            ).all()

        for document, stored_file in rows:
            text, pages, error = "", None, None
            try:
                data = await self._storage.read(stored_file.storage_key)
                if document.doc_type == DocumentType.IMAGE:
                    text = await self._llm.describe_image(
                        ImageInput(
                            data=data, mime_type=stored_file.mime_type, filename=f"image.{_ext(stored_file)}"
                        ),
                        load_prompt("image_description"),
                        ctx=CallContext(purpose="profile.image", project_id=project_id, user_id=user_id),
                    )
                else:
                    extracted = await asyncio.to_thread(extract_text, DocumentType(document.doc_type), data)
                    text, pages = extracted.text, extracted.page_count
                    if not text and extracted.warnings:
                        error = extracted.warnings[-1]
            except (AppError, LLMError, OSError) as exc:
                error = str(exc) or type(exc).__name__

            async with self._sf() as session:
                stored = await session.get(BusinessDocument, document.id)
                if stored is None:
                    continue
                stored.processed_at = datetime.now(UTC)
                stored.page_count = pages
                if text.strip():
                    stored.extracted_text = text
                    stored.status = SourceStatus.PROCESSED
                    stored.error = None
                    report.documents.append(stored.title)
                else:
                    stored.status = SourceStatus.FAILED
                    stored.error = error or "Не удалось извлечь текст"
                    report.failures.append(f"Документ «{stored.title}»: {stored.error}")
                await session.commit()

    # --- 3. Факты -----------------------------------------------------------------------------

    async def _extract_facts(self, project_id: UUID, user_id: UUID | None, report: AnalysisReport) -> bool:
        """Возвращает True, если все новые источники разобраны."""
        segments = await self._unanalyzed_segments(project_id)
        if not segments:
            return True

        async with self._sf() as session:
            existing = {
                (row.field, normalize_for_match(row.value))
                for row in await session.scalars(
                    select(ProfileFact).where(ProfileFact.project_id == project_id)
                )
            }

        ctx = CallContext(purpose="profile.extract", project_id=project_id, user_id=user_id)
        for chunk in make_chunks(segments):
            try:
                result = await self._analyst.extract_facts(chunk, ctx=ctx)
            except LLMError as exc:
                report.failures.append(f"Не удалось разобрать часть источников: {exc}")
                return False  # источники останутся неразобранными и будут обработаны при следующем запуске
            report.facts_rejected += result.rejected
            if result.suspicious:
                report.suspicious_sources.extend(sorted({s.label for s in chunk}))
            async with self._sf() as session:
                for fact in result.facts:
                    key = (fact.field.value, normalize_for_match(fact.value))
                    if key in existing:
                        continue
                    existing.add(key)
                    session.add(
                        ProfileFact(
                            project_id=project_id,
                            field=fact.field,
                            value=fact.value,
                            quote=fact.quote,
                            source_kind=fact.kind,
                            source_id=fact.source_id,
                            source_label=fact.label[:512],
                            status=fact.status,
                        )
                    )
                    report.facts_added += 1
                await session.commit()

        await self._mark_analyzed(segments)
        return True

    async def _unanalyzed_segments(self, project_id: UUID) -> list[SourceSegment]:
        async with self._sf() as session:
            notes = await session.scalars(
                select(BusinessNote)
                .where(BusinessNote.project_id == project_id, BusinessNote.analyzed_at.is_(None))
                .order_by(BusinessNote.created_at)
            )
            pages = await session.scalars(
                select(WebsitePage)
                .where(WebsitePage.project_id == project_id, WebsitePage.analyzed_at.is_(None))
                .order_by(WebsitePage.created_at)
            )
            documents = await session.scalars(
                select(BusinessDocument).where(
                    BusinessDocument.project_id == project_id,
                    BusinessDocument.status == SourceStatus.PROCESSED,
                    BusinessDocument.analyzed_at.is_(None),
                )
            )
            segments = [
                SourceSegment(
                    SourceKind.NOTE, n.id, f"Сообщение владельца от {n.created_at:%d.%m.%Y}", n.text
                )
                for n in notes
            ]
            for page in pages:
                extra = structured_to_text(page.structured_data)
                segments.append(
                    SourceSegment(
                        SourceKind.WEBSITE_PAGE,
                        page.id,
                        f"Сайт: {page.title or unquote(page.url)}",
                        page.text + (f"\n{extra}" if extra else ""),
                    )
                )
            segments.extend(
                SourceSegment(SourceKind.DOCUMENT, d.id, f"Документ: {d.title}", d.extracted_text or "")
                for d in documents
            )
        return segments

    async def _mark_analyzed(self, segments: list[SourceSegment]) -> None:
        now = datetime.now(UTC)
        ids: dict[SourceKind, set[UUID]] = {kind: set() for kind in SourceKind}
        for segment in segments:
            ids[segment.kind].add(segment.source_id)
        async with self._sf() as session:
            await session.execute(
                update(BusinessNote).where(BusinessNote.id.in_(ids[SourceKind.NOTE])).values(analyzed_at=now)
            )
            await session.execute(
                update(WebsitePage)
                .where(WebsitePage.id.in_(ids[SourceKind.WEBSITE_PAGE]))
                .values(analyzed_at=now)
            )
            await session.execute(
                update(BusinessDocument)
                .where(BusinessDocument.id.in_(ids[SourceKind.DOCUMENT]))
                .values(analyzed_at=now)
            )
            await session.commit()

    async def _has_unanalyzed(self, project_id: UUID) -> bool:
        return bool(await self._unanalyzed_segments(project_id))

    # --- 4. Профиль ---------------------------------------------------------------------------

    async def _synthesize(self, project_id: UUID, user_id: UUID | None, report: AnalysisReport) -> None:
        async with self._sf() as session:
            facts = list(
                await session.scalars(
                    select(ProfileFact)
                    .where(ProfileFact.project_id == project_id)
                    .order_by(ProfileFact.status, ProfileFact.created_at.desc())  # confirmed раньше inferred
                )
            )
        if not facts:
            report.questions = [FALLBACK_QUESTIONS[f] for f in KEY_FIELDS]
            return

        selected: list[ProfileFact] = []
        per_field: dict[str, int] = {}
        for fact in facts:
            if per_field.get(fact.field, 0) < MAX_FACTS_PER_FIELD:
                per_field[fact.field] = per_field.get(fact.field, 0) + 1
                selected.append(fact)
        numbered = {i: fact for i, fact in enumerate(selected, start=1)}
        known = [
            KnownFact(i, ProfileField(f.field), f.value, FactStatus(f.status), f.source_label, f.quote)
            for i, f in numbered.items()
        ]

        try:
            synthesis = await self._analyst.synthesize(
                known, ctx=CallContext(purpose="profile.synthesize", project_id=project_id, user_id=user_id)
            )
        except LLMError as exc:
            report.failures.append(f"Не удалось собрать профиль: {exc}")
            await self._load_latest(project_id, report)
            return

        data = BusinessProfileData(summary=synthesis.summary.strip()[:800] or None)
        data.fields = build_profile_fields(synthesis, numbered)

        questions = synthesis.questions or [
            FALLBACK_QUESTIONS[f] for f in KEY_FIELDS if not data.get(f).value
        ]
        async with self._sf() as session:
            profile = await session.scalar(
                select(BusinessProfile).where(BusinessProfile.project_id == project_id)
            )
            if profile is None:
                profile = BusinessProfile(project_id=project_id, latest_version_no=0, completeness=0)
                session.add(profile)
                await session.flush()
            profile.latest_version_no += 1
            profile.completeness = data.completeness
            session.add(
                BusinessProfileVersion(
                    profile_id=profile.id,
                    version_no=profile.latest_version_no,
                    data=data.model_dump(mode="json"),
                    questions=questions,
                    created_by_type=ActorType.AI,
                    change_summary=f"Фактов всего: {len(facts)}, новых: {report.facts_added}",
                )
            )
            await session.commit()
            report.profile_version = profile.latest_version_no
        report.profile = data
        report.questions = questions

    async def _has_profile(self, project_id: UUID) -> bool:
        async with self._sf() as session:
            return (
                await session.scalar(
                    select(BusinessProfile.id).where(BusinessProfile.project_id == project_id)
                )
            ) is not None

    async def _load_latest(self, project_id: UUID, report: AnalysisReport) -> None:
        async with self._sf() as session:
            latest = await load_latest_profile(session, project_id)
        if latest is not None:
            report.profile = BusinessProfileData.model_validate(latest.data)
            report.profile_version = latest.version_no
            report.questions = list(latest.questions)


MAX_FALLBACK_FACTS = 4
SUPPORT_THRESHOLD = 0.6  # доля значимых слов формулировки, подтверждённых фактами пункта
_WORD_RE = re.compile(r"[a-zа-я0-9]+")


def build_profile_fields(
    synthesis: ProfileSynthesis, numbered: dict[int, ProfileFact]
) -> dict[ProfileField, ProfileFieldValue]:
    """Пункты профиля только на основе проверенных фактов.

    1. Формулировка модели со ссылками на существующие факты принимается.
    2. Если ссылок нет (GigaChat иногда возвращает пустые fact_ids), формулировка
       принимается, только если её значимые слова подтверждаются фактами этого пункта.
    3. Иначе значение собирается из фактов пункта без повторов — без доверия к тексту модели.
    """
    by_field: dict[ProfileField, list[ProfileFact]] = {}
    for fact in numbered.values():
        by_field.setdefault(ProfileField(fact.field), []).append(fact)

    chosen: dict[ProfileField, tuple[str, list[ProfileFact]]] = {}
    for item in synthesis.fields:
        try:
            name = ProfileField(item.field.strip().lower())
        except ValueError:
            continue
        value = item.value.strip()[:600]
        if not value:
            continue
        refs = [numbered[i] for i in dict.fromkeys(item.fact_ids) if i in numbered]
        if refs:
            chosen[name] = (value, refs)
        elif by_field.get(name) and is_supported(value, by_field[name]):
            chosen[name] = (value, by_field[name][:5])

    fields: dict[ProfileField, ProfileFieldValue] = {}
    for name in ProfileField:
        if name in chosen:
            value, refs = chosen[name]
        elif by_field.get(name):
            refs = _distinct_facts(by_field[name])[:MAX_FALLBACK_FACTS]
            value = "; ".join(f.value for f in refs)[:600]
        else:
            continue
        fields[name] = ProfileFieldValue(
            value=value,
            status=FactStatus.CONFIRMED
            if any(r.status == FactStatus.CONFIRMED for r in refs)
            else FactStatus.INFERRED,
            sources=[
                SourceRef(kind=SourceKind(r.source_kind), id=r.source_id, label=r.source_label, quote=r.quote)
                for r in refs[:5]
            ],
        )
    return fields


def _stems(text: str) -> list[str]:
    """Значимые слова, обрезанные до 5 букв: грубая замена морфологии для русского."""
    return [w[:5] for w in _WORD_RE.findall(normalize_for_match(text)) if len(w) >= 4 or w.isdigit()]


def is_supported(value: str, facts: list[ProfileFact]) -> bool:
    words = _stems(value)
    if not words:
        return False
    basis = set(_stems(" ".join(f"{f.value} {f.quote}" for f in facts)))
    return sum(1 for w in words if w in basis) / len(words) >= SUPPORT_THRESHOLD


def _distinct_facts(facts: list[ProfileFact]) -> list[ProfileFact]:
    """Факты без повторов: если формулировка одного содержится в другом, оставляем один."""
    kept: list[ProfileFact] = []
    for fact in facts:  # подтверждённые владельцем идут первыми
        norm = normalize_for_match(fact.value)
        if any(norm in normalize_for_match(k.value) or normalize_for_match(k.value) in norm for k in kept):
            continue
        kept.append(fact)
    return kept


async def load_latest_profile(session: AsyncSession, project_id: UUID) -> BusinessProfileVersion | None:
    return await session.scalar(
        select(BusinessProfileVersion)
        .join(BusinessProfile, BusinessProfile.id == BusinessProfileVersion.profile_id)
        .where(BusinessProfile.project_id == project_id)
        .order_by(BusinessProfileVersion.version_no.desc())
        .limit(1)
    )


def _crawl_error(errors: list[str], skipped: list[str]) -> str:
    if skipped and not errors:
        return "владелец сайта запретил его автоматическое чтение в robots.txt"
    if errors:
        return "не удалось прочитать ни одной страницы (" + errors[0].split(": ", 1)[-1] + ")"
    return "на страницах не нашлось текста"


def _ext(stored: StoredFile) -> str:
    return stored.storage_key.rsplit(".", 1)[-1] if "." in stored.storage_key else "jpg"


FIELD_ORDER = list(FIELD_LABELS)
