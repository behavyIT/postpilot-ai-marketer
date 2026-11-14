"""Фоновая задача: изучить новые источники проекта и прислать профиль бизнеса.

Несколько источников, присланных подряд, обрабатываются одним запуском: задача
ждёт несколько секунд перед стартом, а повторная постановка в очередь, пока задача
ждёт, отбрасывается (ключ queued в Redis). Запуски по одному проекту не пересекаются
(блокировка в Redis).
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from taskiq import Context, TaskiqDepends

from app.agents.orchestrator.profile_view import analysis_summary, profile_reply
from app.agents.orchestrator.replies import Reply
from app.core.logging import get_logger
from app.workers.broker import broker
from app.workers.deps import WorkerDeps

log = get_logger(__name__)

MAX_RERUNS = 2


def queued_key(project_id: UUID | str) -> str:
    return f"profile:queued:{project_id}"


def lock_key(project_id: UUID | str) -> str:
    return f"profile:lock:{project_id}"


@broker.task(task_name="profile.analyze")
async def analyze_profile(
    project_id: str,
    user_id: str,
    chat_id: int | None,
    force: bool = False,
    context: Context = TaskiqDepends(),
) -> None:
    deps: WorkerDeps = context.state.deps
    await run_profile_analysis(deps, UUID(project_id), UUID(user_id), chat_id, force=force)


async def run_profile_analysis(
    deps: WorkerDeps, project_id: UUID, user_id: UUID, chat_id: int | None, *, force: bool = False
) -> None:
    await asyncio.sleep(deps.settings.profile_batch_delay_s)
    await deps.redis.delete(queued_key(project_id))
    try:
        async with deps.redis.lock(lock_key(project_id), timeout=1800, blocking_timeout=1800):
            report = await deps.profile_service.run(project_id, user_id=user_id, force=force)
            reruns = 0
            while report.has_pending_sources and reruns < MAX_RERUNS:
                reruns += 1  # пока шёл анализ, пользователь прислал ещё источники
                report = await deps.profile_service.run(project_id, user_id=user_id)
    except Exception:
        log.exception("profile.analysis_failed", project_id=str(project_id))
        if chat_id is not None:
            await deps.notifier.send(
                chat_id,
                Reply(
                    text="Не получилось изучить источники — что-то пошло не так. Попробуйте позже: /profile"
                ),
            )
        return

    if chat_id is None:
        return
    summary = analysis_summary(
        websites=report.websites,
        documents=report.documents,
        facts_added=report.facts_added,
        facts_rejected=report.facts_rejected,
        failures=report.failures,
        suspicious=report.suspicious_sources,
    )
    if report.profile is not None:
        reply = profile_reply(
            report.project_name, report.profile, version=report.profile_version, questions=report.questions
        )
        reply.text = f"{summary}\n\n{reply.text}" if summary else reply.text
    else:
        questions = "\n".join(f"{i}. {q}" for i, q in enumerate(report.questions, start=1))
        reply = Reply(
            text=(summary + "\n\n" if summary else "")
            + "Пока мало сведений, чтобы составить профиль. Расскажите о бизнесе:\n"
            + questions,
            intent="show_profile",
        )
    await deps.notifier.send(chat_id, reply)
