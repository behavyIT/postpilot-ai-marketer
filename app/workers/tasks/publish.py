"""Фоновые задачи публикации: отправка поста в канал и разбор зависших публикаций."""

from __future__ import annotations

from uuid import UUID

from taskiq import Context, TaskiqDepends

from app.agents.orchestrator.post_view import outcome_reply
from app.core.logging import get_logger
from app.services.publishing import PublishOutcome
from app.workers.broker import broker
from app.workers.deps import WorkerDeps

log = get_logger(__name__)


def publish_queued_key(variant_id: UUID | str, version_no: int) -> str:
    return f"publish:queued:{variant_id}:{version_no}"


STALE_CHECK_KEY = "publish:stale_check"


@broker.task(task_name="publish.variant")
async def publish_variant(variant_id: str, version_no: int, context: Context = TaskiqDepends()) -> None:
    deps: WorkerDeps = context.state.deps
    await run_publish(deps, UUID(variant_id), version_no)


async def run_publish(deps: WorkerDeps, variant_id: UUID, version_no: int) -> None:
    try:
        outcome = await deps.publishing_service.publish(variant_id)
        await notify_outcome(deps, outcome)
    except Exception:
        # Статус в БД остаётся PUBLISHING — диспетчер разберёт его как зависший и спросит владельца.
        log.exception("publish.task_failed", variant_id=str(variant_id))
    finally:
        await deps.redis.delete(publish_queued_key(variant_id, version_no))


@broker.task(task_name="publish.resolve_stale")
async def resolve_stale(context: Context = TaskiqDepends()) -> None:
    deps: WorkerDeps = context.state.deps
    try:
        for outcome in await deps.publishing_service.resolve_stale():
            await notify_outcome(deps, outcome)
    finally:
        await deps.redis.delete(STALE_CHECK_KEY)


async def notify_outcome(deps: WorkerDeps, outcome: PublishOutcome) -> None:
    reply = outcome_reply(outcome)
    if reply is None:
        log.info("publish.skipped", variant_id=str(outcome.variant_id), reason=outcome.reason)
        return
    for chat_id in outcome.notify:
        try:
            await deps.notifier.send(chat_id, reply)
        except Exception:
            log.exception("publish.notify_failed", chat_id=chat_id)
