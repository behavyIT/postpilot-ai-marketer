"""Фоновая задача: стратегия, контент-план, тексты постов, правки и картинки с отправкой результата в чат."""

from __future__ import annotations

from uuid import UUID

from taskiq import Context, TaskiqDepends

from app.agents.orchestrator.content_view import plan_reply, strategy_reply
from app.agents.orchestrator.messages import q
from app.agents.orchestrator.post_view import post_card
from app.agents.orchestrator.posts import load_image
from app.agents.orchestrator.replies import Action, Button, Reply
from app.ai.llm.base import LLMError
from app.core.errors import AppError
from app.core.logging import get_logger
from app.db.models import Project
from app.domain.enums import Platform
from app.domain.publications.refs import PostRef
from app.services.jobs import ContentJob, ContentJobKind
from app.services.publications import PublicationService, active_account
from app.workers.broker import broker
from app.workers.deps import WorkerDeps

log = get_logger(__name__)


def content_queued_key(project_id: UUID | str, kind: str, variant_id: UUID | str | None = None) -> str:
    """Ключ «задача уже в очереди». Правки и картинки — по каждому посту отдельно."""
    suffix = f":{variant_id}" if variant_id else ""
    return f"content:queued:{project_id}:{kind}{suffix}"


@broker.task(task_name="content.generate")
async def generate_content(
    kind: str,
    project_id: str,
    user_id: str,
    chat_id: int | None,
    days: int | None = None,
    instructions: str | None = None,
    plan_id: str | None = None,
    topic: str | None = None,
    variant_id: str | None = None,
    context: Context = TaskiqDepends(),
) -> None:
    deps: WorkerDeps = context.state.deps
    job = ContentJob(
        kind=ContentJobKind(kind),
        project_id=UUID(project_id),
        user_id=UUID(user_id),
        chat_id=chat_id,
        days=days,
        instructions=instructions,
        plan_id=UUID(plan_id) if plan_id else None,
        topic=topic,
        variant_id=UUID(variant_id) if variant_id else None,
    )
    await run_content_job(deps, job)


async def run_content_job(deps: WorkerDeps, job: ContentJob) -> None:
    try:
        async with deps.redis.lock(f"content:lock:{job.project_id}", timeout=1800, blocking_timeout=1800):
            await _run(deps, job)
    except AppError as exc:  # в т.ч. «нет профиля» — текст понятен пользователю
        await _notify(deps, job, Reply(text=str(exc)))
    except LLMError as exc:
        await _notify(deps, job, Reply(text=f"Не получилось: {exc}"))
    except Exception:
        log.exception("content.job_failed", kind=job.kind, project_id=str(job.project_id))
        await _notify(
            deps, job, Reply(text="Что-то пошло не так при подготовке контента. Попробуйте ещё раз.")
        )
    finally:
        await deps.redis.delete(content_queued_key(job.project_id, job.kind, job.variant_id))


async def _run(deps: WorkerDeps, job: ContentJob) -> None:
    service = deps.content_service
    name, timezone = await _project_info(deps, job.project_id)

    if job.kind is ContentJobKind.STRATEGY:
        strategy, data = await service.generate_strategy(
            job.project_id, job.user_id, instructions=job.instructions
        )
        await _notify(
            deps,
            job,
            strategy_reply(
                name, data, strategy_id=str(strategy.id), version=strategy.version_no, status=strategy.status
            ),
        )
        return

    if job.kind is ContentJobKind.PLAN:
        outcome = await service.generate_plan(
            job.project_id, job.user_id, days=job.days or 7, instructions=job.instructions
        )
        if outcome.strategy_created:
            await _notify(
                deps,
                job,
                Reply(
                    text=(
                        "Стратегии ещё не было — составил черновик и построил план по нему. "
                        "Посмотреть: /strategy"
                    )
                ),
            )
        await _notify(
            deps, job, plan_reply(name, outcome.plan, outcome.items, timezone=timezone, notes=outcome.notes)
        )
        return

    if job.kind is ContentJobKind.PLAN_POSTS and job.plan_id is not None:
        written = 0
        async for draft in service.write_plan_posts(job.project_id, job.user_id, job.plan_id):
            if draft.error or draft.ref is None:
                await _notify(
                    deps,
                    job,
                    Reply(text=f"Остановился на посте «{q(draft.publication.title)}»: {draft.error}"),
                )
                break
            written += 1
            await _send_card(deps, job, draft.ref, head=f"<b>{written}. «{q(draft.publication.title)}»</b>")
        await _notify(deps, job, await _posts_summary(deps, job, written))
        return

    if job.kind is ContentJobKind.SINGLE_POST and job.topic:
        draft = await service.write_single_post(
            job.project_id, job.user_id, topic=job.topic, instructions=job.instructions
        )
        if draft.error or draft.ref is None:
            await _notify(deps, job, Reply(text=f"Не получилось написать пост: {draft.error}"))
            return
        await _send_card(deps, job, draft.ref, head=None)
        return

    if job.kind in {ContentJobKind.REVISE, ContentJobKind.IMAGE} and job.variant_id is not None:
        if job.kind is ContentJobKind.REVISE:
            revision = await service.revise(
                job.project_id, job.user_id, job.variant_id, instructions=job.instructions
            )
            head, failed = "✏️ <b>Новая версия</b>", "Не получилось переписать пост"
        else:
            revision = await service.draw_image(
                job.project_id, job.user_id, job.variant_id, request=job.topic
            )
            head, failed = "🖼 <b>С картинкой — новая версия</b>", "Не получилось нарисовать картинку"
        if revision.error is None and revision.ref is not None:
            await _send_card(deps, job, revision.ref, head=head)
            return
        buttons = []
        if revision.ref is not None:  # пост ждёт правки — можно повторить или вернуть как было
            value = revision.ref.value()
            retry = Action.POST_REDO if job.kind is ContentJobKind.REVISE else Action.POST_IMAGE
            buttons = [
                [
                    Button(text="🔄 Попробовать снова", action=retry, value=value),
                    Button(text="↩️ Оставить как было", action=Action.POST_KEEP, value=value),
                ]
            ]
        await _notify(deps, job, Reply(text=f"{failed}: {revision.error}", buttons=buttons))


async def _send_card(deps: WorkerDeps, job: ContentJob, ref: PostRef, *, head: str | None) -> None:
    """Превью поста с кнопками согласования; после отправки пост «ждёт одобрения»."""
    if job.chat_id is None:
        return
    async with deps.session_factory() as session:
        view = await PublicationService(session).get_view(job.project_id, ref.variant_id)
        project = await session.get(Project, job.project_id)
        timezone = project.timezone if project else deps.settings.default_timezone
        reply = post_card(view, timezone=timezone, image=await load_image(deps.storage, view), head=head)
    message_ids = await deps.notifier.send(job.chat_id, reply)
    async with deps.session_factory() as session:
        await PublicationService(session).presented(ref, chat_id=job.chat_id, message_ids=message_ids)
        await session.commit()


async def _posts_summary(deps: WorkerDeps, job: ContentJob, written: int) -> Reply:
    if not written:
        return Reply(
            text="Новых постов для написания нет: у всех пунктов плана уже есть тексты.",
            buttons=[[Button(text="⏳ Посты на одобрении", action=Action.POST_LIST, value="pending")]],
        )
    async with deps.session_factory() as session:
        account = await active_account(session, job.project_id, Platform.TELEGRAM)
    text = (
        f"Готово: написал постов — {written}. Посмотрите каждый: «✅ Одобрить» — и выберем время, "
        "«✏️ Изменить» или просто ответьте на пост, что поправить."
    )
    buttons = [[Button(text="📅 Показать план", action=Action.PLAN_SHOW)]]
    if account is None:
        text += "\n\nЧтобы публиковать, подключите Telegram-канал."
        buttons.append([Button(text="📢 Как подключить канал", action=Action.CHANNEL_HELP)])
    return Reply(text=text, buttons=buttons)


async def _project_info(deps: WorkerDeps, project_id: UUID) -> tuple[str, str]:
    async with deps.session_factory() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise AppError("Проект не найден")
        return project.name, project.timezone


async def _notify(deps: WorkerDeps, job: ContentJob, reply: Reply) -> None:
    if job.chat_id is not None:
        await deps.notifier.send(job.chat_id, reply)
