"""Оркестратор: сообщение пользователя → намерение → действие → ответ.

Сценарии — обычный код с явными шагами. Модель только распознаёт намерение
и не может сама выполнить ни одного действия: всё, что меняет данные, делает код.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.orchestrator import messages as msg
from app.agents.orchestrator.intents import CONFIDENCE_THRESHOLD, Intent, IntentClassifier, IntentResult
from app.agents.orchestrator.replies import Action, Button, Expect, Reply
from app.ai.llm.base import LLMError, LLMMessage
from app.ai.llm.service import CallContext, LLMService
from app.core.config import Settings
from app.core.errors import AppError, NotFoundError
from app.core.logging import get_logger
from app.db.models import Project, User
from app.domain.enums import DocumentType, MessageRole, ProjectStatus
from app.integrations.storage.base import FileStorage
from app.services.access import AccessService
from app.services.conversation import ConversationService
from app.services.sources import SourceService

log = get_logger(__name__)

DEFAULT_PROJECT_NAME = "Мой бизнес"
MAX_URLS_PER_MESSAGE = 3
MIN_NOTE_LENGTH_WITH_URL = 30
HISTORY_LIMIT = 6

_COMING_SOON = {
    Intent.CREATE_STRATEGY,
    Intent.CREATE_PLAN,
    Intent.CREATE_POST,
    Intent.EDIT_POST,
    Intent.GENERATE_IMAGE,
    Intent.SCHEDULE_POST,
    Intent.SHOW_SCHEDULE,
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


@dataclass(slots=True)
class Turn:
    """Контекст одного обращения пользователя."""

    user: User
    project: Project | None
    telegram_message_id: int | None = None


class Orchestrator:
    def __init__(
        self,
        session: AsyncSession,
        *,
        llm: LLMService,
        storage: FileStorage,
        settings: Settings,
    ) -> None:
        self._access = AccessService(session, default_timezone=settings.default_timezone)
        self._sources = SourceService(session, storage, max_upload_bytes=settings.max_upload_mb * 1024 * 1024)
        self._conversation = ConversationService(session)
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
                    f"в источники проекта «{msg.q(project.name)}».\n\n"
                    "Можно прислать ещё файлы или ссылку на сайт. Изучу источники и составлю профиль "
                    "бизнеса — эта функция появится в ближайшем обновлении."
                )
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
            return Reply(text=msg.HELP, intent="help")
        except (ValueError, NotFoundError):
            return Reply(text="Проект не найден — откройте список заново: /project", intent="error")

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
            return self._settings(turn)
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
        return Reply(
            text=(
                f"{head}\n\n"
                "Чтобы я узнал бизнес лучше, пришлите ссылку на сайт и документы: презентацию, прайс, "
                "описание услуг. Из всех источников я составлю профиль бизнеса — это появится "
                "в ближайшем обновлении."
            ),
            intent=Intent.DESCRIBE_BUSINESS,
        )

    async def _add_websites(self, turn: Turn, text: str, urls: list[str]) -> Reply:
        project, created_project = await self._ensure_project(turn, None)
        lines: list[str] = []
        for raw in urls[:MAX_URLS_PER_MESSAGE]:
            try:
                website, created = await self._sources.add_website(turn.user, project, raw)
            except AppError as exc:
                lines.append(f"✗ {msg.q(raw)} — {exc}")
                continue
            lines.append(("✓ " if created else "• уже был: ") + msg.q(website.url))

        remainder = text
        for raw in urls:
            remainder = remainder.replace(raw, " ")
        if len(remainder.strip()) >= MIN_NOTE_LENGTH_WITH_URL:
            await self._sources.add_note(
                turn.user, project, text, telegram_message_id=turn.telegram_message_id
            )

        return Reply(
            text=(
                f"{_created_prefix(project, created_project)}Сайты в проекте «{msg.q(project.name)}»:\n"
                + "\n".join(lines)
                + "\n\nИзучу их вместе с остальными источниками — "
                "анализ сайтов появится в ближайшем обновлении."
            ),
            intent=Intent.ADD_WEBSITE,
        )

    async def _show_profile(self, turn: Turn) -> Reply:
        if turn.project is None:
            return Reply(text=msg.NO_PROJECT, intent=Intent.SHOW_PROFILE)
        summary = await self._sources.summary(turn.project.id)
        if summary.total == 0:
            return Reply(
                text=f"Про «{msg.q(turn.project.name)}» я пока ничего не знаю. {msg.ASK_ABOUT_BUSINESS}",
                intent=Intent.SHOW_PROFILE,
            )
        recent = "\n".join(f"• {msg.q(title)}" for title in summary.recent_titles)
        return Reply(
            text=(
                f"<b>{msg.q(turn.project.name)}</b>\n\n"
                f"Собрано источников: сайтов — {summary.websites}, документов — {summary.documents}, "
                f"сообщений о бизнесе — {summary.notes}.\n"
                + (f"\nПоследние:\n{recent}\n" if recent else "")
                + "\nПрофиль бизнеса ещё не составлен: анализ источников появится в ближайшем обновлении."
            ),
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

    def _settings(self, turn: Turn) -> Reply:
        if turn.project is None:
            return Reply(text=msg.NO_PROJECT, intent=Intent.SETTINGS)
        project = turn.project
        return Reply(
            text=(
                f"<b>Настройки проекта «{msg.q(project.name)}»</b>\n\n"
                f"Состояние: {msg.STATUS_LABELS.get(project.status, project.status)}\n"
                f"Часовой пояс: {msg.q(project.timezone)}\n"
                "Режим: ассистент — публикую только то, что вы одобрили\n"
                "Площадки: пока не подключены (Telegram-канал — в ближайших обновлениях)\n"
                "Языковая модель: GigaChat"
            ),
            intent=Intent.SETTINGS,
        )

    # --- Вспомогательное ----------------------------------------------------------------------

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
