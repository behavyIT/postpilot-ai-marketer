"""Сценарии работы с постами: согласование, правки, картинки, расписание.

Одобрить пост можно только кнопкой под конкретной версией: свободный текст вроде
«опубликуй» одобрением не считается. Любая правка возвращает пост на согласование.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.orchestrator.content_view import when_label
from app.agents.orchestrator.intents import Intent, IntentResult
from app.agents.orchestrator.messages import q
from app.agents.orchestrator.post_view import (
    CHANNEL_HOWTO,
    ImageData,
    approved_reply,
    channel_preview,
    overview_reply,
    post_card,
    quick_time,
    schedule_options,
    scheduled_reply,
)
from app.agents.orchestrator.replies import Action, Button, Expect, Reply
from app.core.errors import DomainValidationError
from app.db.models import Project, User
from app.domain.enums import ActorType, Platform, ProjectStatus, PublicationStatus
from app.domain.publications.refs import PostRef
from app.domain.timeparse import parse_when
from app.integrations.storage.base import FileStorage
from app.services.channels import ChannelService
from app.services.content_generation import review_post_text
from app.services.jobs import ContentJob, ContentJobKind, JobQueue, PublishJob
from app.services.publications import (
    ChannelMissingError,
    PostView,
    PublicationService,
    active_account,
)

S = PublicationStatus
MAX_CARDS = 5
_MANUAL_PREFIX = re.compile(r"^\s*текст\s*:\s*", re.IGNORECASE)
_HASHTAG = re.compile(r"#[\w]+")

GROUPS: dict[str, tuple[str, set[PublicationStatus]]] = {
    "pending": ("⏳ Ждут одобрения", {S.DRAFT, S.PENDING_APPROVAL, S.NEEDS_REVISION}),
    "approved": ("✅ Одобрены, без времени", {S.APPROVED}),
    "scheduled": ("📅 Запланированные", {S.SCHEDULED}),
    "failed": ("⚠️ Не вышли", {S.FAILED}),
}
# Ответ на сообщение с постом — правка этого поста, кроме этих намерений.
_REPLY_PASSTHROUGH = {
    Intent.SMALLTALK,
    Intent.HELP,
    Intent.SHOW_PROFILE,
    Intent.SHOW_SCHEDULE,
    Intent.LIST_PROJECTS,
    Intent.SWITCH_PROJECT,
    Intent.CREATE_PROJECT,
    Intent.PAUSE,
    Intent.RESUME,
    Intent.SETTINGS,
    Intent.ANALYTICS,
    Intent.CREATE_PLAN,
    Intent.CREATE_STRATEGY,
    Intent.CONNECT_CHANNEL,
}
POST_TARGETED = {Intent.EDIT_POST, Intent.GENERATE_IMAGE, Intent.SCHEDULE_POST, Intent.CANCEL_POST}

ASK_WHICH_POST = (
    "Какой пост? Откройте его через /schedule и нажмите кнопку под ним или ответьте на сообщение с постом."
)
ASK_TIME = (
    "Когда публикуем? Напишите, например: «завтра в 12:00», «15.11 18:30», "
    "«в пятницу вечером», «через 2 часа»."
)


class PostFlows:
    def __init__(self, session: AsyncSession, *, jobs: JobQueue, storage: FileStorage) -> None:
        self._session = session
        self._jobs = jobs
        self._storage = storage
        self._posts = PublicationService(session)
        self._channels = ChannelService(session)

    # --- Показ -----------------------------------------------------------------------------------

    async def card(self, project: Project, variant_id: UUID, *, head: str | None = None) -> Reply:
        view = await self._posts.get_view(project.id, variant_id)
        return post_card(view, timezone=project.timezone, image=await self._image(view), head=head)

    async def _image(self, view: PostView) -> ImageData | None:
        return await load_image(self._storage, view)

    async def presented(self, chat_id: int, reply: Reply, message_ids: list[int]) -> None:
        """Бот отправил превью: запоминаем сообщения (для ответов-правок) и статус «ждёт одобрения»."""
        if reply.post_ref is not None and message_ids:
            await self._posts.presented(reply.post_ref, chat_id=chat_id, message_ids=message_ids)

    async def overview(self, project: Project) -> Reply:
        counts = {
            group: await self._posts.count(project.id, statuses) for group, (_, statuses) in GROUPS.items()
        }
        return overview_reply(
            project.name,
            timezone=project.timezone,
            account=await self._channels.current(project.id),
            scheduled=await self._posts.list_views(project.id, {S.SCHEDULED}, limit=10),
            published=await self._posts.list_views(project.id, {S.PUBLISHED}, limit=5, newest_first=True),
            counts=counts,
            paused=project.status == ProjectStatus.PAUSED,
        )

    async def list_group(self, project: Project, group: str) -> Reply:
        if group not in GROUPS:
            raise ValueError(group)
        label, statuses = GROUPS[group]
        views = await self._posts.list_views(project.id, statuses, limit=MAX_CARDS)
        if not views:
            return Reply(text=f"{label}: таких постов нет.", intent="post_list")
        cards = [post_card(v, timezone=project.timezone, image=await self._image(v)) for v in views]
        total = await self._posts.count(project.id, statuses)
        more = f" Показываю первые {MAX_CARDS}." if total > MAX_CARDS else ""
        return Reply(text=f"{label}: {total}.{more}", followups=cards, intent="post_list")

    # --- Кнопки ----------------------------------------------------------------------------------

    async def button(
        self, user: User, project: Project, action: Action, value: str, *, chat_id: int | None
    ) -> Reply:
        if action is Action.CHANNEL_HELP:
            return Reply(text=CHANNEL_HOWTO, intent="channel_help")
        if action is Action.POST_LIST:
            return await self.list_group(project, value)
        if action is Action.POST_OPEN:
            return await self.card(project, UUID(hex=value))
        ref = PostRef.parse(value)
        try:
            return await self._post_button(user, project, action, ref, value, chat_id=chat_id)
        except ChannelMissingError:
            return Reply(text=f"Публиковать пока некуда.\n\n{CHANNEL_HOWTO}", intent="channel_missing")

    async def _post_button(
        self, user: User, project: Project, action: Action, ref: PostRef, value: str, *, chat_id: int | None
    ) -> Reply:
        posts = self._posts
        now = datetime.now(UTC)
        if action is Action.POST_APPROVE:
            view = await posts.approve(user, project.id, ref)
            account = await active_account(self._session, project.id, Platform(view.variant.platform))
            return approved_reply(view, timezone=project.timezone, account=account, now=now)
        if action is Action.POST_REJECT:
            view = await posts.reject(user, project.id, ref)
            return Reply(
                text=f"❌ Отклонил «{q(view.publication.title)}». Можно попросить другой вариант.",
                buttons=[[Button(text="🔄 Переписать", action=Action.POST_REDO, value=ref.value())]],
                close_source=True,
                intent="post_rejected",
            )
        if action is Action.POST_EDIT:
            view = await posts.get_view(project.id, ref.variant_id)
            return Reply(
                text=(
                    f"Что изменить в «{q(view.publication.title)}»? Например: «короче и с юмором», "
                    "«добавь про доставку», «убери эмодзи».\n\n"
                    "Чтобы заменить текст целиком, пришлите его, начав со слова «Текст:». "
                    "/cancel — передумали."
                ),
                expect=Expect.POST_EDIT,
                expect_data={"ref": ref.value()},
                intent="post_edit",
            )
        if action is Action.POST_REDO:
            return await self._revise(
                user, project, ref.variant_id, instructions=None, chat_id=chat_id, close_source=True
            )
        if action is Action.POST_IMAGE:
            return await self._draw(
                user, project, ref.variant_id, request=None, chat_id=chat_id, close_source=True
            )
        if action is Action.POST_NO_IMAGE:
            view = await posts.start_revision(user, project.id, ref.variant_id, request="без картинки")
            await posts.add_version(
                view.variant,
                body=view.version.body,
                hashtags=list(view.version.hashtags),
                actor=ActorType.USER,
                user_id=user.id,
                change_request="без картинки",
                used_fact_ids=list(view.version.used_fact_ids),
                warnings=list(view.version.warnings),
            )
            reply = await self.card(project, ref.variant_id, head="🗑 Картинку убрал — новая версия:")
            reply.close_source = True
            return reply
        if action is Action.POST_KEEP:
            view = await posts.get_view(project.id, ref.variant_id, lock=True)
            if view.variant.status == S.NEEDS_REVISION:
                await posts.transition(
                    view.variant,
                    S.PENDING_APPROVAL,
                    actor=ActorType.USER,
                    user_id=user.id,
                    reason="правка отменена",
                )
            reply = await self.card(project, ref.variant_id)
            reply.close_source = True
            return reply
        if action is Action.POST_PREVIEW:
            view = await posts.get_view(project.id, ref.variant_id)
            return channel_preview(view, await self._image(view))
        if action is Action.POST_WHEN:
            view = await posts.get_view(project.id, ref.variant_id)
            if view.variant.status not in {S.APPROVED, S.SCHEDULED}:
                raise DomainValidationError("Сначала одобрите пост — без одобрения публиковать не буду")
            return Reply(
                text=f"Когда публикуем «{q(view.publication.title)}»?",
                buttons=schedule_options(view, timezone=project.timezone, now=now),
                intent="post_when",
            )
        if action is Action.POST_AT:
            option = value.split(".")[2] if value.count(".") >= 2 else ""
            if option == "custom":
                return Reply(
                    text=ASK_TIME,
                    expect=Expect.POST_TIME,
                    expect_data={"ref": ref.value()},
                    intent="post_when",
                )
            at = await self._option_time(project, ref, option, now)
            return await self._schedule(user, project, ref, at, now=now)
        if action is Action.POST_NOW:
            return await self._schedule(user, project, ref, now, now=now)
        if action is Action.POST_UNSCHEDULE:
            view = await posts.unschedule(user, project.id, ref.variant_id)
            value_now = PostRef(view.variant.id, view.version.version_no).value()
            return Reply(
                text=f"⏹ Снял «{q(view.publication.title)}» с расписания. Пост остался одобренным.",
                buttons=[
                    [
                        Button(text="📅 Запланировать", action=Action.POST_WHEN, value=value_now),
                        Button(text="🚀 Опубликовать сейчас", action=Action.POST_NOW, value=value_now),
                    ]
                ],
                close_source=True,
                intent="post_unscheduled",
            )
        if action is Action.POST_RETRY:
            view = await posts.retry(user, project, ref.variant_id, now=now)
            await self._jobs.enqueue(PublishJob(view.variant.id, view.version.version_no))
            return Reply(
                text=f"🔁 Пробую опубликовать «{q(view.publication.title)}» снова…",
                close_source=True,
                intent="post_retry",
            )
        if action is Action.POST_DONE:
            view = await posts.confirm_published(user, project.id, ref.variant_id)
            return Reply(
                text=f"Отметил «{q(view.publication.title)}» как опубликованный.",
                close_source=True,
                intent="post_done",
            )
        if action is Action.POST_CANCEL:
            view = await posts.cancel(user, project.id, ref.variant_id)
            return Reply(
                text=f"🚫 Пост «{q(view.publication.title)}» отменён.",
                close_source=True,
                intent="post_cancel",
            )
        raise ValueError(action)

    async def _option_time(self, project: Project, ref: PostRef, option: str, now: datetime) -> datetime:
        if option == "plan":
            view = await self._posts.get_view(project.id, ref.variant_id)
            if view.publication.planned_at is None:
                raise DomainValidationError("У этого поста нет даты в плане — выберите время")
            return view.publication.planned_at
        at = quick_time(option, now.astimezone(ZoneInfo(project.timezone)))
        if at is None:
            raise DomainValidationError("Это время уже прошло — выберите другое")
        return at

    async def _schedule(
        self, user: User, project: Project, ref: PostRef, at: datetime, *, now: datetime
    ) -> Reply:
        view = await self._posts.schedule(user, project, ref, at, now=now)
        due_now = view.variant.scheduled_at is not None and view.variant.scheduled_at <= now + timedelta(
            minutes=1
        )
        if due_now and project.status == ProjectStatus.ACTIVE:
            await self._jobs.enqueue(PublishJob(view.variant.id, view.version.version_no))
            channel = f" в «{q(view.account.title)}»" if view.account else ""
            return Reply(
                text=f"🚀 Публикую «{q(view.publication.title)}»{channel}…",
                close_source=True,
                intent="post_publish_now",
            )
        return scheduled_reply(view, timezone=project.timezone, paused=project.status == ProjectStatus.PAUSED)

    # --- Текст: правки и время ---------------------------------------------------------------------

    async def edit(
        self,
        user: User,
        project: Project,
        variant_id: UUID,
        text: str,
        *,
        chat_id: int | None,
        html: str | None = None,
    ) -> Reply:
        manual = _MANUAL_PREFIX.match(text)
        if manual is None:
            return await self._revise(
                user, project, variant_id, instructions=text.strip()[:1000], chat_id=chat_id
            )
        body = _MANUAL_PREFIX.sub("", html if html is not None else q(text), count=1).strip()
        if not body:
            raise DomainValidationError("После слова «Текст:» пришлите сам текст поста")
        view = await self._posts.start_revision(user, project.id, variant_id, request="текст владельца")
        await self._posts.add_version(
            view.variant,
            body=body,
            hashtags=_HASHTAG.findall(body)[:10],
            actor=ActorType.USER,
            user_id=user.id,
            change_request="текст владельца",
            warnings=await review_post_text(self._session, project.id, body),
            media=view.media,
            image_prompt=view.version.image_prompt,
        )
        return await self.card(project, variant_id, head="✏️ Ваш текст — новая версия:")

    async def schedule_text(self, user: User, project: Project, ref: PostRef, text: str) -> Reply:
        now = datetime.now(UTC)
        at = parse_when(text, now.astimezone(ZoneInfo(project.timezone)))
        if at is None:
            return Reply(
                text="Не понял время. " + ASK_TIME,
                expect=Expect.POST_TIME,
                expect_data={"ref": ref.value()},
                intent="post_when",
            )
        try:
            return await self._schedule(user, project, ref, at, now=now)
        except ChannelMissingError:
            return Reply(text=f"Публиковать пока некуда.\n\n{CHANNEL_HOWTO}", intent="channel_missing")

    async def _revise(
        self,
        user: User,
        project: Project,
        variant_id: UUID,
        *,
        instructions: str | None,
        chat_id: int | None,
        close_source: bool = False,
    ) -> Reply:
        view = await self._posts.start_revision(
            user, project.id, variant_id, request=instructions or "другой вариант"
        )
        await self._jobs.enqueue(
            ContentJob(
                kind=ContentJobKind.REVISE,
                project_id=project.id,
                user_id=user.id,
                chat_id=chat_id,
                instructions=instructions,
                variant_id=view.variant.id,
            )
        )
        title = q(view.publication.title)
        text = (
            f"✏️ Переписываю «{title}»: {q(instructions)}. Пришлю новую версию через полминуты."
            if instructions
            else f"🔄 Пишу другой вариант «{title}» — пришлю через полминуты."
        )
        return Reply(text=text, close_source=close_source, intent="post_revise")

    async def _draw(
        self,
        user: User,
        project: Project,
        variant_id: UUID,
        *,
        request: str | None,
        chat_id: int | None,
        close_source: bool = False,
    ) -> Reply:
        view = await self._posts.start_revision(user, project.id, variant_id, request="картинка")
        await self._jobs.enqueue(
            ContentJob(
                kind=ContentJobKind.IMAGE,
                project_id=project.id,
                user_id=user.id,
                chat_id=chat_id,
                topic=request,
                variant_id=view.variant.id,
            )
        )
        return Reply(
            text=f"🖼 Рисую картинку для «{q(view.publication.title)}» — это около 30 секунд.",
            close_source=close_source,
            intent="post_image",
        )

    # --- Свободный текст ---------------------------------------------------------------------------

    async def on_intent(
        self,
        user: User,
        project: Project,
        result: IntentResult,
        text: str,
        *,
        target: UUID | None,
        chat_id: int | None,
    ) -> Reply:
        """Намерения про конкретный пост. target — пост, на который ответили, или последний показанный."""
        intent = result.intent
        if intent is Intent.CANCEL_POST:
            return await self._cancel_choices(project)
        if target is None:
            if intent is Intent.SCHEDULE_POST:
                return await self.list_group(project, "approved")
            return Reply(text=ASK_WHICH_POST, intent=intent)
        view = await self._posts.get_view(project.id, target)
        if intent is Intent.GENERATE_IMAGE:
            return await self._draw(user, project, target, request=result.topic, chat_id=chat_id)
        if intent is Intent.SCHEDULE_POST:
            if view.variant.status not in {S.APPROVED, S.SCHEDULED}:
                card = await self.card(project, target)
                return Reply(
                    text=(
                        f"Пост «{q(view.publication.title)}» ещё не одобрен. Посмотрите его и нажмите "
                        "«✅ Одобрить» — после этого выберем время."
                    ),
                    followups=[card],
                    intent=intent,
                )
            ref = PostRef(view.variant.id, view.version.version_no)
            if not (result.when or "").strip() and parse_when(text, datetime.now(UTC)) is None:
                return Reply(
                    text=f"Когда публикуем «{q(view.publication.title)}»?",
                    buttons=schedule_options(view, timezone=project.timezone, now=datetime.now(UTC)),
                    intent=intent,
                )
            return await self.schedule_text(user, project, ref, result.when or text)
        # EDIT_POST и всё остальное в ответ на пост — правка.
        return await self.edit(user, project, target, result.instructions or text, chat_id=chat_id)

    async def _cancel_choices(self, project: Project) -> Reply:
        views = await self._posts.list_views(project.id, {S.SCHEDULED}, limit=MAX_CARDS)
        if not views:
            return Reply(text="Запланированных публикаций нет — отменять нечего.", intent=Intent.CANCEL_POST)
        return Reply(
            text=(
                "Какую публикацию снять с расписания? Пост останется одобренным — "
                "его можно будет запланировать заново."
            ),
            buttons=[
                [
                    Button(
                        text=f"⏹ {when_label(v.variant.scheduled_at, project.timezone)} — "
                        f"{v.publication.title}",
                        action=Action.POST_UNSCHEDULE,
                        value=PostRef(v.variant.id, v.version.version_no).value(),
                    )
                ]
                for v in views
            ],
            intent=Intent.CANCEL_POST,
        )

    async def focused(self, chat_id: int | None, project: Project) -> UUID | None:
        return await self._posts.focused(chat_id, project.id) if chat_id is not None else None

    async def ref_for_message(self, chat_id: int, message_id: int) -> PostRef | None:
        return await self._posts.ref_for_message(chat_id, message_id)


async def load_image(storage: FileStorage, view: PostView) -> ImageData | None:
    """Картинка поста для превью; если файл пропал, превью покажем без неё."""
    if view.media is None:
        return None
    try:
        data = await storage.read(view.media.storage_key)
    except OSError:
        return None
    return ImageData(data=data, name=view.media.original_name or "image.jpg")


def reply_is_post_edit(intent: Intent) -> bool:
    return intent not in _REPLY_PASSTHROUGH
