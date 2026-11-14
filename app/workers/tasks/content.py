"""Фоновая задача: стратегия, контент-план и тексты постов с отправкой результата в чат."""

from __future__ import annotations

from uuid import UUID

from taskiq import Context, TaskiqDepends

from app.agents.orchestrator.content_view import plan_reply, post_preview, strategy_reply
from app.agents.orchestrator.replies import Action, Button, Reply
from app.ai.llm.base import LLMError
from app.core.errors import AppError
from app.core.logging import get_logger
from app.db.models import Project
from app.services.jobs import ContentJob, ContentJobKind
from app.workers.broker import broker
from app.workers.deps import WorkerDeps

log = get_logger(__name__)


def content_queued_key(project_id: UUID | str, kind: str) -> str:
    return f"content:queued:{project_id}:{kind}"


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
        await deps.redis.delete(content_queued_key(job.project_id, job.kind))


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
        index = 0
        async for draft in service.write_plan_posts(job.project_id, job.user_id, job.plan_id):
            index += 1
            if draft.error:
                await _notify(
                    deps, job, Reply(text=f"Остановился на посте «{draft.publication.title}»: {draft.error}")
                )
                break
            written += 1
            await _notify(
                deps,
                job,
                post_preview(
                    draft.publication,
                    draft.text or "",
                    warnings=draft.warnings,
                    timezone=timezone,
                    index=index,
                ),
            )
        summary = (
            f"Готово: написал черновиков — {written}. "
            "Согласование и публикация в канал — в следующем обновлении."
            if written
            else "Новых постов для написания нет: у всех пунктов плана уже есть тексты."
        )
        await _notify(
            deps,
            job,
            Reply(
                text=summary,
                buttons=[[Button(text="📅 Показать план", action=Action.PLAN_SHOW)]],
            ),
        )
        return

    if job.kind is ContentJobKind.SINGLE_POST and job.topic:
        draft = await service.write_single_post(
            job.project_id, job.user_id, topic=job.topic, instructions=job.instructions
        )
        if draft.error:
            await _notify(deps, job, Reply(text=f"Не получилось написать пост: {draft.error}"))
            return
        await _notify(
            deps,
            job,
            post_preview(draft.publication, draft.text or "", warnings=draft.warnings, timezone=timezone),
        )


async def _project_info(deps: WorkerDeps, project_id: UUID) -> tuple[str, str]:
    async with deps.session_factory() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise AppError("Проект не найден")
        return project.name, project.timezone


async def _notify(deps: WorkerDeps, job: ContentJob, reply: Reply) -> None:
    if job.chat_id is not None:
        await deps.notifier.send(job.chat_id, reply)
