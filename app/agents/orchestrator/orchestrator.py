"""Оркестратор: сообщение пользователя → намерение → действие → ответ.

Сценарии — обычный код с явными шагами. Модель только распознаёт намерение
и не может сама выполнить ни одного действия: всё, что меняет данные, делает код.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.orchestrator import messages as msg
from app.agents.orchestrator.content_view import plan_reply, strategy_reply
from app.agents.orchestrator.intents import CONFIDENCE_THRESHOLD, Intent, IntentClassifier, IntentResult
from app.agents.orchestrator.profile_view import profile_reply
from app.agents.orchestrator.replies import Action, Button, Expect, Reply
from app.ai.llm.base import LLMError, LLMMessage
from app.ai.llm.service import CallContext, LLMService
from app.core.config import Settings
from app.core.errors import AppError, NotFoundError
from app.core.logging import get_logger
from app.db.models import Project, User
from app.domain.enums import DocumentType, MessageRole, ProjectStatus
from app.integrations.storage.base import FileStorage
from app.schemas.business_profile import BusinessProfileData
from app.schemas.content import StrategyData
from app.services.access import AccessService
from app.services.content_repo import ContentRepository
from app.services.conversation import ConversationService
from app.services.jobs import ContentJob, ContentJobKind, JobQueue, ProfileAnalysisJob
from app.services.memory import MemoryService
from app.services.profile_analysis import load_latest_profile
from app.services.sources import SourceService

log = get_logger(__name__)

DEFAULT_PROJECT_NAME = "Мой бизнес"
MAX_URLS_PER_MESSAGE = 3
MIN_NOTE_LENGTH_WITH_URL = 30
HISTORY_LIMIT = 6

_COMING_SOON = {
    Intent.EDIT_POST,
    Intent.GENERATE_IMAGE,
    Intent.SCHEDULE_POST,
    Intent.CANCEL_POST,
    Intent.ANALYTICS,
    Intent.FIND_TOPICS,
}
_DOC_LABELS = {
    DocumentType.PDF: "PDF",
    DocumentType.DOCX: "DOCX",
    DocumentType.PPTX: "PPTX",
    DocumentType.TXT: "текст",
    DocumentType.IMAGE: "изображение",
}


_CONTENT_ACTIONS = {
    Action.STRATEGY_APPROVE,
    Action.STRATEGY_REDO,
    Action.PLAN_CREATE,
    Action.PLAN_APPROVE,
    Action.PLAN_REDO,
    Action.PLAN_WRITE,
    Action.PLAN_SHOW,
    Action.MEMORY_FORGET,
}


@dataclass(slots=True)
class Turn:
    """Контекст одного обращения пользователя."""

    user: User
    project: Project | None
    telegram_message_id: int | None = None
    chat_id: int | None = None  # куда воркер пришлёт результат фоновой задачи


class Orchestrator:
    def __init__(
        self,
        session: AsyncSession,
        *,
        llm: LLMService,
        storage: FileStorage,
        settings: Settings,
        jobs: JobQueue,
    ) -> None:
        self._session = session
        self._jobs = jobs
        self._access = AccessService(session, default_timezone=settings.default_timezone)
        self._sources = SourceService(session, storage, max_upload_bytes=settings.max_upload_mb * 1024 * 1024)
        self._conversation = ConversationService(session)
        self._content = ContentRepository(session)
        self._memory = MemoryService(session)
        self._classifier = IntentClassifier(llm)

    # --- Точки входа --------------------------------------------------------------------------

    async def handle_text(self, turn: Turn, text: str, *, urls: list[str] | None = None) -> Reply:
        history = await self._history(turn)
        await self._remember(turn, MessageRole.USER, text)
        try:
            if urls:
                reply = await self._add_websites(turn, text, urls)
            else:
                result = await self._classifier.classify(
                    text,
                    history=history,
                    ctx=CallContext(
                        purpose="intent",
                        user_id=turn.user.id,
                        project_id=turn.project.id if turn.project else None,
                    ),
                )
                log.info("orchestrator.intent", intent=result.intent, confidence=result.confidence)
                reply = await self._dispatch(turn, result, text)
        except LLMError as exc:
            reply = Reply(text=str(exc), intent="error")
        except AppError as exc:
            reply = Reply(text=str(exc), intent="error")
        await self._remember(turn, MessageRole.ASSISTANT, reply.text, intent=reply.intent)
        return reply

    async def handle_command(self, turn: Turn, intent: Intent, command_text: str) -> Reply:
        """Команда меню — то же намерение, только без модели."""
        await self._remember(turn, MessageRole.USER, command_text)
        try:
            reply = await self._dispatch(turn, IntentResult(intent=intent, confidence=1.0), command_text)
        except AppError as exc:
            reply = Reply(text=str(exc), intent="error")
        await self._remember(turn, MessageRole.ASSISTANT, reply.text, intent=reply.intent)
        return reply

    async def handle_document(
        self, turn: Turn, *, filename: str | None, data: bytes, caption: str | None
    ) -> Reply:
        try:
            project, created_project = await self._ensure_project(turn, None)
            document, created = await self._sources.add_document(
                turn.user, project, filename=filename, data=data
            )
            if caption and caption.strip():
                await self._sources.add_note(
                    turn.user, project, caption, telegram_message_id=turn.telegram_message_id
                )
            prefix = _created_prefix(project, created_project)
            if not created:
                text = f"{prefix}Файл «{msg.q(document.title)}» уже есть в проекте."
            else:
                label = _DOC_LABELS.get(DocumentType(document.doc_type), document.doc_type)
                text = (
                    f"{prefix}Сохранил «{msg.q(document.title)}» ({label}, {_size(len(data))}) "
                    f"в источники проекта «{msg.q(project.name)}».\n\n{msg.ANALYSIS_STARTED}"
                )
                await self._schedule_analysis(turn, project)
            reply = Reply(text=text, intent="add_document")
        except AppError as exc:
            reply = Reply(text=str(exc), intent="error")
        await self._remember(turn, MessageRole.ASSISTANT, reply.text, intent=reply.intent)
        return reply

    async def handle_button(self, turn: Turn, action: Action, value: str) -> Reply:
        reply = await self._button_reply(turn, action, value)
        # Ответ на кнопку попадает в историю: модель видит, например, что проект переключён.
        await self._remember(turn, MessageRole.ASSISTANT, reply.text, intent=reply.intent)
        return reply

    async def _button_reply(self, turn: Turn, action: Action, value: str) -> Reply:
        try:
            if action is Action.PROJECT_SWITCH:
                project = await self._access.set_active_project(turn.user, UUID(value))
                turn.project = project
                return Reply(
                    text=f"Активный проект: <b>{msg.q(project.name)}</b>.",
                    intent="switch_project",
                )
            if action is Action.PROJECT_NEW:
                return Reply(text=msg.ASK_PROJECT_NAME, expect=Expect.PROJECT_NAME, intent="create_project")
            if action is Action.PROJECT_LIST:
                return await self._list_projects(turn)
            if action is Action.PROFILE_SHOW:
                return await self._show_profile(turn)
            if action is Action.PROFILE_REFRESH:
                if turn.project is None:
                    return Reply(text=msg.NO_PROJECT, intent="error")
                await self._schedule_analysis(turn, turn.project, force=True)
                return Reply(text=msg.PROFILE_REFRESHING, intent="profile_refresh")
            if action in _CONTENT_ACTIONS:
                return await self._content_button(turn, action, value)
            return Reply(text=msg.HELP, intent="help")
        except NotFoundError as exc:
            return Reply(text=f"{exc} — откройте список заново", intent="error")
        except ValueError:
            # Испорченные или устаревшие данные кнопки: технические подробности пользователю не нужны.
            return Reply(text="Кнопка устарела — откройте раздел заново через меню.", intent="error")
        except AppError as exc:
            return Reply(text=str(exc), intent="error")

    async def create_project(self, turn: Turn, name: str) -> Reply:
        """Ответ на вопрос «Как назвать проект?»."""
        try:
            project = await self._access.create_project(turn.user, name)
        except AppError as exc:
            return Reply(text=f"{exc}. {msg.ASK_PROJECT_NAME}", expect=Expect.PROJECT_NAME, intent="error")
        turn.project = project
        reply = Reply(
            text=(
                f"Создал проект <b>{msg.q(project.name)}</b> и сделал его активным.\n\n"
                f"{msg.ASK_ABOUT_BUSINESS}"
            ),
            intent="create_project",
        )
        await self._remember(turn, MessageRole.ASSISTANT, reply.text, intent=reply.intent)
        return reply

    # --- Сценарии -----------------------------------------------------------------------------

    async def _dispatch(self, turn: Turn, result: IntentResult, text: str) -> Reply:
        intent = result.intent
        if (
            result.confidence < CONFIDENCE_THRESHOLD
            and result.clarification_question
            and intent not in {Intent.SMALLTALK, Intent.HELP}
        ):
            return Reply(text=msg.q(result.clarification_question), intent="clarify")

        if intent is Intent.DESCRIBE_BUSINESS:
            return await self._describe_business(turn, result, text)
        if intent is Intent.ADD_WEBSITE:
            return Reply(text=msg.ASK_WEBSITE, intent=intent)
        if intent is Intent.SHOW_PROFILE:
            return await self._show_profile(turn)
        if intent is Intent.CREATE_PROJECT:
            if result.project_name:
                return await self.create_project(turn, result.project_name)
            return Reply(text=msg.ASK_PROJECT_NAME, expect=Expect.PROJECT_NAME, intent=intent)
        if intent is Intent.SWITCH_PROJECT:
            return await self._switch_project(turn, result.project_name)
        if intent is Intent.LIST_PROJECTS:
            return await self._list_projects(turn)
        if intent in {Intent.PAUSE, Intent.RESUME}:
            return await self._set_paused(turn, paused=intent is Intent.PAUSE)
        if intent is Intent.SETTINGS:
            return await self._settings(turn)
        if intent is Intent.ADD_PREFERENCE:
            return await self._add_preference(turn, result.instructions or text)
        if intent is Intent.CREATE_STRATEGY:
            return await self._strategy(turn, result.instructions, from_command=text.startswith("/"))
        if intent is Intent.CREATE_PLAN:
            return await self._create_plan(turn, result.period_days or 7, result.instructions)
        if intent is Intent.SHOW_SCHEDULE:
            return await self._show_plan(turn)
        if intent is Intent.CREATE_POST:
            return await self._create_post(turn, result.topic, result.instructions)
        if intent is Intent.HELP:
            return Reply(text=msg.HELP, intent=intent)
        if intent is Intent.SMALLTALK:
            return Reply(text=msg.SMALLTALK, intent=intent)
        if intent in _COMING_SOON:
            return Reply(text=msg.coming_soon(result), intent=intent)
        return Reply(text=msg.UNKNOWN, intent=Intent.UNKNOWN)

    async def _describe_business(self, turn: Turn, result: IntentResult, text: str) -> Reply:
        project, created = await self._ensure_project(turn, result.business_name)
        await self._sources.add_note(turn.user, project, text, telegram_message_id=turn.telegram_message_id)
        head = (
            f"Создал проект «{msg.q(project.name)}» и записал это туда."
            if created
            else f"Записал это в проект «{msg.q(project.name)}»."
        )
        await self._schedule_analysis(turn, project)
        return Reply(text=f"{head}\n\n{msg.NOTE_SAVED_HINT}", intent=Intent.DESCRIBE_BUSINESS)

    async def _add_websites(self, turn: Turn, text: str, urls: list[str]) -> Reply:
        project, created_project = await self._ensure_project(turn, None)
        lines: list[str] = []
        added = False
        for raw in urls[:MAX_URLS_PER_MESSAGE]:
            try:
                website, created = await self._sources.add_website(turn.user, project, raw)
            except AppError as exc:
                lines.append(f"✗ {msg.q(raw)} — {exc}")
                continue
            lines.append(("✓ " if created else "• уже был: ") + msg.q(website.url))
            added = added or created

        remainder = text
        for raw in urls:
            remainder = remainder.replace(raw, " ")
        if len(remainder.strip()) >= MIN_NOTE_LENGTH_WITH_URL:
            await self._sources.add_note(
                turn.user, project, text, telegram_message_id=turn.telegram_message_id
            )
            added = True

        if added:
            await self._schedule_analysis(turn, project)
        return Reply(
            text=(
                f"{_created_prefix(project, created_project)}Сайты в проекте «{msg.q(project.name)}»:\n"
                + "\n".join(lines)
                + (f"\n\n{msg.ANALYSIS_STARTED}" if added else "")
            ),
            intent=Intent.ADD_WEBSITE,
        )

    async def _show_profile(self, turn: Turn) -> Reply:
        if turn.project is None:
            return Reply(text=msg.NO_PROJECT, intent=Intent.SHOW_PROFILE)
        latest = await load_latest_profile(self._session, turn.project.id)
        if latest is not None:
            return profile_reply(
                turn.project.name,
                BusinessProfileData.model_validate(latest.data),
                version=latest.version_no,
                questions=list(latest.questions),
            )
        summary = await self._sources.summary(turn.project.id)
        if summary.total == 0:
            return Reply(
                text=f"Про «{msg.q(turn.project.name)}» я пока ничего не знаю. {msg.ASK_ABOUT_BUSINESS}",
                intent=Intent.SHOW_PROFILE,
            )
        return Reply(
            text=(
                f"<b>{msg.q(turn.project.name)}</b>\n\n"
                f"Собрано источников: сайтов — {summary.websites}, документов — {summary.documents}, "
                f"сообщений о бизнесе — {summary.notes}.\n\n"
                "Профиль бизнеса ещё не готов. Если я его не прислал, нажмите кнопку — изучу источники."
            ),
            buttons=[[Button(text="🔍 Изучить источники", action=Action.PROFILE_REFRESH)]],
            intent=Intent.SHOW_PROFILE,
        )

    async def _list_projects(self, turn: Turn) -> Reply:
        projects = await self._access.list_projects(turn.user)
        new_button = [Button(text="➕ Новый проект", action=Action.PROJECT_NEW)]
        if not projects:
            return Reply(text=msg.NO_PROJECT, buttons=[new_button], intent=Intent.LIST_PROJECTS)
        active_id = turn.project.id if turn.project else None
        rows = [
            [
                Button(
                    text=("✅ " if p.id == active_id else "")
                    + ("⏸ " if p.status == ProjectStatus.PAUSED else "")
                    + p.name,
                    action=Action.PROJECT_SWITCH,
                    value=str(p.id),
                )
            ]
            for p in projects
        ]
        return Reply(
            text="Ваши проекты. Нажмите, чтобы сделать проект активным:",
            buttons=[*rows, new_button],
            intent=Intent.LIST_PROJECTS,
        )

    async def _switch_project(self, turn: Turn, name: str | None) -> Reply:
        if name:
            matches = await self._access.find_projects_by_name(turn.user, name)
            if len(matches) == 1:
                project = await self._access.set_active_project(turn.user, matches[0].id)
                turn.project = project
                return Reply(
                    text=f"Активный проект: <b>{msg.q(project.name)}</b>.", intent=Intent.SWITCH_PROJECT
                )
        return await self._list_projects(turn)

    async def _set_paused(self, turn: Turn, *, paused: bool) -> Reply:
        if turn.project is None:
            return Reply(text=msg.NO_PROJECT, intent=Intent.PAUSE if paused else Intent.RESUME)
        status = ProjectStatus.PAUSED if paused else ProjectStatus.ACTIVE
        project = await self._access.set_project_status(turn.user, turn.project.id, status)
        turn.project = project
        if paused:
            text = (
                f"Автоматическая работа проекта «{msg.q(project.name)}» на паузе: "
                "ничего не будет готовиться и публиковаться по расписанию. Возобновить — /resume."
            )
            return Reply(text=text, intent=Intent.PAUSE)
        return Reply(
            text=f"Автоматическая работа проекта «{msg.q(project.name)}» возобновлена.", intent=Intent.RESUME
        )

    async def _settings(self, turn: Turn) -> Reply:
        if turn.project is None:
            return Reply(text=msg.NO_PROJECT, intent=Intent.SETTINGS)
        project = turn.project
        preferences = await self._memory.preferences(project.id)
        remembered = (
            "\n\n<b>Что я запомнил о ваших пожеланиях</b>\n" + "\n".join(f"• {msg.q(p)}" for p in preferences)
            if preferences
            else ""
        )
        return Reply(
            text=(
                f"<b>Настройки проекта «{msg.q(project.name)}»</b>\n\n"
                f"Состояние: {msg.STATUS_LABELS.get(project.status, project.status)}\n"
                f"Часовой пояс: {msg.q(project.timezone)}\n"
                "Режим: ассистент — публикую только то, что вы одобрили\n"
                "Площадки: пока не подключены (Telegram-канал — в ближайших обновлениях)\n"
                "Языковая модель: GigaChat" + remembered + f"\n\n{msg.PREFERENCE_HINT}"
            ),
            buttons=[[Button(text="🧹 Забыть пожелания", action=Action.MEMORY_FORGET)]]
            if preferences
            else [],
            intent=Intent.SETTINGS,
        )

    # --- Стратегия, план, посты -----------------------------------------------------------------

    async def _add_preference(self, turn: Turn, wish: str) -> Reply:
        if turn.project is None:
            return Reply(text=msg.NO_PROJECT, intent=Intent.ADD_PREFERENCE)
        saved = await self._memory.add_preference(turn.project.id, wish, source="chat")
        buttons = []
        if await self._content.latest_strategy(turn.project.id) is not None:
            buttons.append([Button(text="🔄 Обновить стратегию", action=Action.STRATEGY_REDO)])
        return Reply(
            text=msg.preference_saved(" ".join(wish.split())[:500], duplicate=not saved),
            buttons=buttons,
            intent=Intent.ADD_PREFERENCE,
        )

    async def _require_profile(self, turn: Turn) -> Reply | None:
        """Ответ-заглушка, если профиля ещё нет; None — можно продолжать."""
        if turn.project is None:
            return Reply(text=msg.NEED_PROFILE, intent="need_profile")
        if await load_latest_profile(self._session, turn.project.id) is not None:
            return None
        summary = await self._sources.summary(turn.project.id)
        if summary.total:
            await self._schedule_analysis(turn, turn.project)
            return Reply(text=msg.PROFILE_FIRST, intent="need_profile")
        return Reply(text=msg.NEED_PROFILE, intent="need_profile")

    async def _strategy(self, turn: Turn, instructions: str | None, *, from_command: bool) -> Reply:
        blocked = await self._require_profile(turn)
        if blocked is not None or turn.project is None:
            return blocked or Reply(text=msg.NEED_PROFILE)
        existing = await self._content.latest_strategy(turn.project.id)
        if existing is not None and not instructions and from_command:
            return strategy_reply(
                turn.project.name,
                StrategyData.model_validate(existing.data),
                strategy_id=str(existing.id),
                version=existing.version_no,
                status=existing.status,
            )
        await self._enqueue_content(turn, ContentJobKind.STRATEGY, instructions=instructions)
        return Reply(text=msg.STRATEGY_STARTED, intent=Intent.CREATE_STRATEGY)

    async def _create_plan(self, turn: Turn, days: int, instructions: str | None) -> Reply:
        blocked = await self._require_profile(turn)
        if blocked is not None:
            return blocked
        await self._enqueue_content(turn, ContentJobKind.PLAN, days=days, instructions=instructions)
        return Reply(text=msg.plan_started(days), intent=Intent.CREATE_PLAN)

    async def _show_plan(self, turn: Turn) -> Reply:
        if turn.project is None:
            return Reply(text=msg.NO_PROJECT, intent=Intent.SHOW_SCHEDULE)
        view = await self._content.latest_plan(turn.project.id)
        if view is None:
            return Reply(
                text=msg.NO_PLAN,
                buttons=[[Button(text="📅 Составить план на неделю", action=Action.PLAN_CREATE)]],
                intent=Intent.SHOW_SCHEDULE,
            )
        return plan_reply(
            turn.project.name,
            view.plan,
            view.items,
            timezone=turn.project.timezone,
            written=set(view.written),
        )

    async def _create_post(self, turn: Turn, topic: str | None, instructions: str | None) -> Reply:
        blocked = await self._require_profile(turn)
        if blocked is not None or turn.project is None:
            return blocked or Reply(text=msg.NEED_PROFILE)
        if topic:
            await self._enqueue_content(
                turn, ContentJobKind.SINGLE_POST, topic=topic, instructions=instructions
            )
            return Reply(text=msg.POST_STARTED, intent=Intent.CREATE_POST)
        view = await self._content.latest_plan(turn.project.id)
        if view is not None and len(view.written) < len(view.items):
            return Reply(
                text=msg.ASK_POST_TOPIC + "\n\nИли напишу тексты для постов из контент-плана.",
                buttons=[
                    [
                        Button(
                            text="✍️ Написать тексты по плану",
                            action=Action.PLAN_WRITE,
                            value=str(view.plan.id),
                        )
                    ]
                ],
                intent=Intent.CREATE_POST,
            )
        return Reply(text=msg.ASK_POST_TOPIC, intent=Intent.CREATE_POST)

    async def _content_button(self, turn: Turn, action: Action, value: str) -> Reply:
        if turn.project is None:
            return Reply(text=msg.NO_PROJECT, intent="error")
        project = turn.project
        if action is Action.STRATEGY_APPROVE:
            strategy = await self._content.approve_strategy(turn.user, project.id, UUID(value))
            return Reply(
                text=f"Стратегия утверждена (вариант {strategy.version_no}). Составить по ней контент-план?",
                buttons=[[Button(text="📅 Составить контент-план", action=Action.PLAN_CREATE)]],
                intent="strategy_approved",
            )
        if action is Action.STRATEGY_REDO:
            return await self._strategy(turn, None, from_command=False)
        if action is Action.PLAN_CREATE:
            return await self._create_plan(turn, 7, None)
        if action is Action.PLAN_SHOW:
            return await self._show_plan(turn)
        if action is Action.MEMORY_FORGET:
            await self._memory.forget_all(project.id)
            return Reply(text=msg.MEMORY_FORGOTTEN, intent="memory_forget")
        plan = await self._content.get_plan(project.id, UUID(value))
        if action is Action.PLAN_APPROVE:
            await self._content.approve_plan(turn.user, project.id, plan.id)
            view = await self._content.plan_view(plan)
            return Reply(
                text=f"План утверждён: публикаций — {len(view.items)}. Написать тексты?",
                buttons=[[Button(text="✍️ Написать тексты", action=Action.PLAN_WRITE, value=str(plan.id))]],
                intent="plan_approved",
            )
        if action is Action.PLAN_REDO:
            days = (plan.period_end - plan.period_start).days + 1
            return await self._create_plan(turn, days, None)
        if action is Action.PLAN_WRITE:
            view = await self._content.plan_view(plan)
            remaining = len(view.items) - len(view.written)
            if remaining == 0:
                return Reply(text="У всех постов этого плана уже есть тексты.", intent="plan_write")
            await self._enqueue_content(turn, ContentJobKind.PLAN_POSTS, plan_id=plan.id)
            return Reply(text=msg.POSTS_STARTED.format(count=remaining), intent="plan_write")
        return Reply(text=msg.HELP, intent="help")

    async def _enqueue_content(
        self,
        turn: Turn,
        kind: ContentJobKind,
        *,
        days: int | None = None,
        instructions: str | None = None,
        plan_id: UUID | None = None,
        topic: str | None = None,
    ) -> None:
        if turn.project is None:
            return
        await self._jobs.enqueue(
            ContentJob(
                kind=kind,
                project_id=turn.project.id,
                user_id=turn.user.id,
                chat_id=turn.chat_id,
                days=days,
                instructions=instructions,
                plan_id=plan_id,
                topic=topic,
            )
        )

    # --- Вспомогательное ----------------------------------------------------------------------

    async def _schedule_analysis(self, turn: Turn, project: Project, *, force: bool = False) -> None:
        """Поставить изучение источников в очередь; задача уйдёт после фиксации транзакции."""
        await self._jobs.enqueue(
            ProfileAnalysisJob(project_id=project.id, user_id=turn.user.id, chat_id=turn.chat_id, force=force)
        )

    async def _ensure_project(self, turn: Turn, name: str | None) -> tuple[Project, bool]:
        if turn.project is not None:
            return turn.project, False
        project = await self._access.create_project(turn.user, name or DEFAULT_PROJECT_NAME)
        turn.project = project
        return project, True

    async def _history(self, turn: Turn) -> list[LLMMessage]:
        rows = await self._conversation.recent(turn.user, limit=HISTORY_LIMIT)
        return [
            LLMMessage(
                role="user" if row.role == MessageRole.USER else "assistant", content=row.content[:500]
            )
            for row in rows
        ]

    async def _remember(self, turn: Turn, role: MessageRole, text: str, *, intent: str | None = None) -> None:
        await self._conversation.add(
            turn.user,
            role,
            text,
            project_id=turn.project.id if turn.project else None,
            intent=intent,
            telegram_message_id=turn.telegram_message_id if role is MessageRole.USER else None,
        )


def _created_prefix(project: Project, created: bool) -> str:
    return f"Создал проект «{msg.q(project.name)}».\n" if created else ""


def _size(num_bytes: int) -> str:
    if num_bytes < 1024 * 1024:
        return f"{max(num_bytes // 1024, 1)} КБ"
    return f"{num_bytes / (1024 * 1024):.1f} МБ".replace(".", ",")
