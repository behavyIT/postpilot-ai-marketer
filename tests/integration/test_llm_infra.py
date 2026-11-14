import asyncio

import pytest
from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.ai.llm.base import LLMBusyError
from app.ai.llm.limiter import RedisLimiter
from app.ai.llm.usage import SqlUsageSink, UsageRecord
from app.db.models import LLMUsage
from app.db.session import create_session_factory
from app.domain.enums import LLMCallStatus

pytestmark = pytest.mark.integration


async def test_redis_limiter_allows_one_request_at_a_time(redis_client: Redis) -> None:
    limiter = RedisLimiter(redis_client, "test:llm:slot", ttl_s=10, wait_s=5)
    active = 0
    peak = 0

    async def call() -> None:
        nonlocal active, peak
        async with limiter.slot():
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.05)
            active -= 1

    await asyncio.gather(*(call() for _ in range(5)))
    assert peak == 1


async def test_redis_limiter_reports_busy_when_queue_wait_is_exceeded(redis_client: Redis) -> None:
    holder = RedisLimiter(redis_client, "test:llm:busy", ttl_s=10, wait_s=5)
    impatient = RedisLimiter(redis_client, "test:llm:busy", ttl_s=10, wait_s=0.2)

    async with holder.slot():
        with pytest.raises(LLMBusyError):
            async with impatient.slot():
                pass


async def test_sql_usage_sink_persists_record(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    sink = SqlUsageSink(factory)

    await sink.record(
        UsageRecord(
            purpose="test.usage_sink",
            provider="fake",
            model="ultra",
            status=LLMCallStatus.OK,
            input_tokens=120,
            output_tokens=30,
            latency_ms=900,
        )
    )

    async with factory() as session:
        row = await session.scalar(select(LLMUsage).where(LLMUsage.purpose == "test.usage_sink"))
        assert row is not None
        assert (row.input_tokens, row.output_tokens, row.latency_ms) == (120, 30, 900)
        await session.execute(delete(LLMUsage).where(LLMUsage.purpose == "test.usage_sink"))
        await session.commit()
