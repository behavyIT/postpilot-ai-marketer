import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.orchestrator.intents import Intent
from app.agents.orchestrator.replies import Action, Expect
from app.ai.llm.base import LLMRequest, LLMUnavailableError
from app.ai.llm.fake import FakeLLMProvider
from app.core.config import Settings
from app.db.models import BusinessNote, ConversationMessage, Project, Website
from app.domain.enums import ProjectStatus
from tests.integration.helpers import docx_bytes, intent, make_orchestrator, make_user, turn_for

pytestmark = pytest.mark.integration


async def test_business_description_creates_project_and_saves_note(
    session: AsyncSession, test_settings: Settings
) -> None:
    user = await make_user(session, 5001)
    provider = FakeLLMProvider(intent("describe_business", business_name="Кофейня Зерно"))
    orchestrator = make_orchestrator(session, test_settings, provider)
    turn = await turn_for(session, user)

    reply = await orchestrator.handle_text(turn, "У нас кофейня «Зерно» в Казани, варим спешелти")

    assert reply.intent == Intent.DESCRIBE_BUSINESS
    assert "Создал проект «Кофейня Зерно» и записал это туда" in reply.text
    assert reply.text.count("Кофейня Зерно") == 1
    assert turn.project is not None
    note = await session.scalar(select(BusinessNote).where(BusinessNote.project_id == turn.project.id))
    assert note is not None
    assert "спешелти" in note.text


async def test_links_are_handled_without_calling_the_model(
    session: AsyncSession, test_settings: Settings
) -> None:
    user = await make_user(session, 5002)
    provider = FakeLLMProvider()  # любой вызов модели упадёт: ответов нет
    orchestrator = make_orchestrator(session, test_settings, provider)
    turn = await turn_for(session, user)

    reply = await orchestrator.handle_text(
        turn, "Наш сайт coffee.ru и ещё http://localhost", urls=["coffee.ru", "http://localhost"]
    )

    assert provider.requests == []
    assert "✓ https://coffee.ru/" in reply.text
    assert "✗ http://localhost" in reply.text
    assert turn.project is not None
    sites = (await session.scalars(select(Website.url).where(Website.project_id == turn.project.id))).all()
    assert sites == ["https://coffee.ru/"]


async def test_create_project_by_name_and_by_question(session: AsyncSession, test_settings: Settings) -> None:
    user = await make_user(session, 5003)
    provider = FakeLLMProvider(intent("create_project", project_name="Пион"), intent("create_project"))
    orchestrator = make_orchestrator(session, test_settings, provider)
    turn = await turn_for(session, user)

    named = await orchestrator.handle_text(turn, "Добавь проект цветочный магазин «Пион»")
    assert "Создал проект <b>Пион</b>" in named.text

    asked = await orchestrator.handle_text(turn, "Хочу ещё один проект")
    assert asked.expect is Expect.PROJECT_NAME
    answered = await orchestrator.create_project(turn, "Автосервис")
    assert answered.expect is None
    assert turn.project is not None and turn.project.name == "Автосервис"

    invalid = await orchestrator.create_project(turn, "   ")
    assert invalid.expect is Expect.PROJECT_NAME


async def test_project_list_buttons_switch_active_project(
    session: AsyncSession, test_settings: Settings
) -> None:
    user = await make_user(session, 5004)
    orchestrator = make_orchestrator(session, test_settings, FakeLLMProvider())
    turn = await turn_for(session, user)
    await orchestrator.create_project(turn, "Первый")
    await orchestrator.create_project(turn, "Второй")

    listing = await orchestrator.handle_command(turn, Intent.LIST_PROJECTS, "/project")
    labels = [row[0].text for row in listing.buttons]
    assert labels == ["✅ Второй", "Первый", "➕ Новый проект"]

    first = listing.buttons[1][0]
    reply = await orchestrator.handle_button(turn, Action(first.action), first.value)
    assert "Активный проект: <b>Первый</b>" in reply.text
    assert turn.project is not None and turn.project.name == "Первый"
    logged = await session.scalar(
        select(ConversationMessage).where(
            ConversationMessage.user_id == user.id, ConversationMessage.intent == "switch_project"
        )
    )
    assert logged is not None and "Первый" in logged.content


async def test_buttons_cannot_switch_to_foreign_project(
    session: AsyncSession, test_settings: Settings
) -> None:
    owner = await make_user(session, 5005)
    stranger = await make_user(session, 5006)
    orchestrator = make_orchestrator(session, test_settings, FakeLLMProvider())
    owner_turn = await turn_for(session, owner)
    await orchestrator.create_project(owner_turn, "Чужой бизнес")
    assert owner_turn.project is not None

    stranger_turn = await turn_for(session, stranger)
    reply = await orchestrator.handle_button(stranger_turn, Action.PROJECT_SWITCH, str(owner_turn.project.id))
    assert "Проект не найден" in reply.text
    assert stranger_turn.project is None

    garbage = await orchestrator.handle_button(stranger_turn, Action.PROJECT_SWITCH, "не-uuid")
    assert "Проект не найден" in garbage.text


async def test_pause_and_resume(session: AsyncSession, test_settings: Settings) -> None:
    user = await make_user(session, 5007)
    orchestrator = make_orchestrator(session, test_settings, FakeLLMProvider())
    turn = await turn_for(session, user)
    await orchestrator.create_project(turn, "Пекарня")

    paused = await orchestrator.handle_command(turn, Intent.PAUSE, "/pause")
    assert "на паузе" in paused.text
    assert turn.project is not None and turn.project.status == ProjectStatus.PAUSED

    resumed = await orchestrator.handle_command(turn, Intent.RESUME, "/resume")
    assert "возобновлена" in resumed.text
    project = await session.get(Project, turn.project.id)
    assert project is not None and project.status == ProjectStatus.ACTIVE


async def test_future_features_show_understanding(session: AsyncSession, test_settings: Settings) -> None:
    user = await make_user(session, 5008)
    provider = FakeLLMProvider(intent("create_plan", period_days=14))
    orchestrator = make_orchestrator(session, test_settings, provider)
    turn = await turn_for(session, user)

    reply = await orchestrator.handle_text(turn, "Составь контент-план на две недели")

    assert "контент-план на две недели" in reply.text
    assert "ближайших обновлениях" in reply.text


async def test_low_confidence_asks_clarifying_question(
    session: AsyncSession, test_settings: Settings
) -> None:
    user = await make_user(session, 5009)
    provider = FakeLLMProvider(
        intent("create_post", confidence=0.3, clarification_question="О чём должен быть пост?")
    )
    orchestrator = make_orchestrator(session, test_settings, provider)

    reply = await orchestrator.handle_text(await turn_for(session, user), "ну это самое")

    assert reply.text == "О чём должен быть пост?"
    assert reply.intent == "clarify"


async def test_model_failure_gives_friendly_message(session: AsyncSession, test_settings: Settings) -> None:
    user = await make_user(session, 5010)
    provider = FakeLLMProvider(LLMUnavailableError("GigaChat временно недоступен, попробуйте позже"))
    orchestrator = make_orchestrator(session, test_settings, provider)

    reply = await orchestrator.handle_text(await turn_for(session, user), "Сделай пост")

    assert reply.text == "GigaChat временно недоступен, попробуйте позже"


async def test_history_is_saved_and_passed_to_classifier(
    session: AsyncSession, test_settings: Settings
) -> None:
    user = await make_user(session, 5011)
    seen: list[LLMRequest] = []

    def responder(request: LLMRequest) -> dict[str, object]:
        seen.append(request)
        if len(seen) == 1:
            return intent("create_plan", confidence=0.4, clarification_question="На какой период?")
        return intent("create_plan", period_days=7)

    orchestrator = make_orchestrator(session, test_settings, FakeLLMProvider(responder=responder))
    turn = await turn_for(session, user)

    await orchestrator.handle_text(turn, "Сделай контент-план")
    reply = await orchestrator.handle_text(turn, "на неделю")

    assert "на неделю" in reply.text
    second_call = [m.content for m in seen[1].messages[1:]]
    assert second_call == ["Сделай контент-план", "На какой период?", "на неделю"]
    stored = await session.scalar(
        select(func.count()).select_from(ConversationMessage).where(ConversationMessage.user_id == user.id)
    )
    assert stored == 4


async def test_documents_are_saved_with_caption(session: AsyncSession, test_settings: Settings) -> None:
    user = await make_user(session, 5012)
    orchestrator = make_orchestrator(session, test_settings, FakeLLMProvider())
    turn = await turn_for(session, user)

    saved = await orchestrator.handle_document(
        turn, filename="Услуги.docx", data=docx_bytes(), caption="Наш прайс на 2026 год"
    )
    duplicate = await orchestrator.handle_document(
        turn, filename="Услуги.docx", data=docx_bytes(), caption=None
    )
    rejected = await orchestrator.handle_document(turn, filename="x.exe", data=b"MZ\x90\x00", caption=None)

    assert "Сохранил «Услуги.docx» (DOCX" in saved.text
    assert "уже есть в проекте" in duplicate.text
    assert rejected.intent == "error"
    assert turn.project is not None
    notes = (
        await session.scalars(select(BusinessNote.text).where(BusinessNote.project_id == turn.project.id))
    ).all()
    assert notes == ["Наш прайс на 2026 год"]


async def test_profile_shows_collected_sources(session: AsyncSession, test_settings: Settings) -> None:
    user = await make_user(session, 5013)
    orchestrator = make_orchestrator(session, test_settings, FakeLLMProvider())
    turn = await turn_for(session, user)

    empty = await orchestrator.handle_command(turn, Intent.SHOW_PROFILE, "/profile")
    assert "нет проекта" in empty.text

    await orchestrator.handle_text(turn, "coffee.ru", urls=["coffee.ru"])
    profile = await orchestrator.handle_command(turn, Intent.SHOW_PROFILE, "/profile")
    assert "сайтов — 1" in profile.text
    assert "Профиль бизнеса ещё не составлен" in profile.text


async def test_user_text_is_html_escaped_in_replies(session: AsyncSession, test_settings: Settings) -> None:
    user = await make_user(session, 5014)
    provider = FakeLLMProvider(intent("create_post", topic="<script>alert(1)</script>"))
    orchestrator = make_orchestrator(session, test_settings, provider)

    reply = await orchestrator.handle_text(await turn_for(session, user), "пост про <script>")

    assert "<script>" not in reply.text
    assert "&lt;script&gt;" in reply.text
