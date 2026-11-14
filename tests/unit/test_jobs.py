from uuid import uuid4

from app.services.jobs import DeferredJobQueue, MemoryJobQueue, ProfileAnalysisJob


async def test_deferred_queue_sends_only_on_flush_and_deduplicates() -> None:
    inner = MemoryJobQueue()
    deferred = DeferredJobQueue(inner)
    job = ProfileAnalysisJob(project_id=uuid4(), user_id=uuid4(), chat_id=1)

    await deferred.enqueue_profile_analysis(job)
    await deferred.enqueue_profile_analysis(job)
    assert inner.profile_jobs == []

    await deferred.flush()
    assert inner.profile_jobs == [job]


async def test_discarded_jobs_are_never_sent() -> None:
    inner = MemoryJobQueue()
    deferred = DeferredJobQueue(inner)
    await deferred.enqueue_profile_analysis(
        ProfileAnalysisJob(project_id=uuid4(), user_id=uuid4(), chat_id=1)
    )
    deferred.discard()
    await deferred.flush()
    assert inner.profile_jobs == []
