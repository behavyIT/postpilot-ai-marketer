"""Запуск изучения из чата, показ профиля и фоновая задача воркера."""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.orchestrator.intents import Intent
from app.agents.orchestrator.replies import Action
from app.ai.llm.fake import FakeLLMProvider
from app.bot.notifier import MemoryNotifier
from app.core.config import Settings
from app.db.models import BusinessProfile, BusinessProfileVersion
from app.domain.enums import FactStatus, ProfileField
from app.schemas.business_profile import BusinessProfileData, ProfileFieldValue
from app.services.jobs import MemoryJobQueue
from app.services.profile_analysis import AnalysisReport
from app.workers.tasks.profile import queued_key, run_profile_analysis
from tests.integration.helpers import docx_bytes, intent, make_orchestrator, make_user, turn_for

pytestmark = pytest.mark.integration


async def test_new_sources_schedule_analysis_with_chat_id(
    session: AsyncSession, test_settings: Settings
) -> None:
    user = await make_user(session, 9001)
    jobs = MemoryJobQueue()
    orchestrator = make_orchestrator(
        session, test_settings, FakeLLMProvider(intent("describe_business", business_name="Зерно")), jobs
    )
    turn = await turn_for(session, user)

    described = await orchestrator.handle_text(turn, "У нас кофейня в Казани")
    await orchestrator.handle_text(turn, "coffee.ru", urls=["coffee.ru"])
    await orchestrator.handle_text(turn, "coffee.ru", urls=["coffee.ru"])  # повтор — без нового анализа
    doc = await orchestrator.handle_document(turn, filename="Услуги.docx", data=docx_bytes(), caption=None)

    assert "пришлю его через минуту" in described.text
    assert "Изучаю — пришлю профиль" in doc.text
    assert len(jobs.profile_jobs) == 3
    assert all(j.chat_id == 777 and j.user_id == user.id and not j.force for j in jobs.profile_jobs)


async def test_profile_command_shows_latest_version_and_refresh_forces(
    session: AsyncSession, test_settings: Settings
) -> None:
    user = await make_user(session, 9002)
    jobs = MemoryJobQueue()
    orchestrator = make_orchestrator(session, test_settings, FakeLLMProvider(), jobs)
    turn = await turn_for(session, user)
    await orchestrator.create_project(turn, "Зерно")
    assert turn.project is not None

    data = BusinessProfileData(summary="Кофейня для студентов")
    data.fields[ProfileField.GEOGRAPHY] = ProfileFieldValue(value="Казань", status=FactStatus.CONFIRMED)
    profile = BusinessProfile(project_id=turn.project.id, latest_version_no=1, completeness=7)
    session.add(profile)
    await session.flush()
    session.add(
        BusinessProfileVersion(
            profile_id=profile.id,
            version_no=1,
            data=data.model_dump(mode="json"),
            questions=["Есть доставка?"],
        )
    )
    await session.flush()

    shown = await orchestrator.handle_command(turn, Intent.SHOW_PROFILE, "/profile")
    assert "Профиль бизнеса «Зерно»" in shown.text
    assert "✅ <b>География:</b> Казань" in shown.text
    assert "1. Есть доставка?" in shown.text
    assert shown.buttons[0][0].action is Action.PROFILE_REFRESH

    refreshed = await orchestrator.handle_button(turn, Action.PROFILE_REFRESH, "")
    assert "Обновляю профиль" in refreshed.text
    assert jobs.profile_jobs[-1].force is True


@dataclass
class FakeProfileService:
    reports: list[AnalysisReport]
    calls: list[dict[str, Any]] = field(default_factory=list)
    error: Exception | None = None

    async def run(self, project_id: UUID, *, user_id: UUID | None, force: bool = False) -> AnalysisReport:
        self.calls.append({"project_id": project_id, "force": force})
        if self.error:
            raise self.error
        return self.reports.pop(0)


@dataclass
class FakeDeps:
    settings: Settings
    redis: Redis
    profile_service: FakeProfileService
    notifier: MemoryNotifier


def report(project_id: UUID, *, pending: bool = False) -> AnalysisReport:
    data = BusinessProfileData(summary="Кофейня")
    data.fields[ProfileField.GEOGRAPHY] = ProfileFieldValue(value="Казань", status=FactStatus.CONFIRMED)
    return AnalysisReport(
        project_id=project_id,
        project_name="Зерно",
        websites=[("https://zerno.example/", 3)],
        facts_added=4,
        profile=data,
        profile_version=1,
        questions=["Кто клиенты?"],
        has_pending_sources=pending,
    )


async def test_worker_task_sends_profile_and_reruns_for_late_sources(
    redis_client: Redis, test_settings: Settings
) -> None:
    project_id = uuid4()
    await redis_client.set(queued_key(project_id), "1")
    svc = FakeProfileService(reports=[report(project_id, pending=True), report(project_id)])
    deps = FakeDeps(
        settings=test_settings.model_copy(update={"profile_batch_delay_s": 0}),
        redis=redis_client,
        profile_service=svc,
        notifier=MemoryNotifier(),
    )

    await run_profile_analysis(deps, project_id, uuid4(), 555, force=True)  # type: ignore[arg-type]

    assert await redis_client.get(queued_key(project_id)) is None
    assert [c["force"] for c in svc.calls] == [True, False]  # второй запуск — для источников, пришедших позже
    chat_id, sent = deps.notifier.sent[0]
    assert chat_id == 555
    assert "Изучил: сайт zerno.example (3 стр.)" in sent.text
    assert "Профиль бизнеса «Зерно»" in sent.text
    assert sent.buttons[0][0].action is Action.PROFILE_REFRESH


async def test_worker_task_reports_failure_to_user(redis_client: Redis, test_settings: Settings) -> None:
    deps = FakeDeps(
        settings=test_settings.model_copy(update={"profile_batch_delay_s": 0}),
        redis=redis_client,
        profile_service=FakeProfileService(reports=[], error=RuntimeError("boom")),
        notifier=MemoryNotifier(),
    )
    await run_profile_analysis(deps, uuid4(), uuid4(), 555)  # type: ignore[arg-type]
    assert "Не получилось изучить источники" in deps.notifier.sent[0][1].text
