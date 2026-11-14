import time
import uuid

import pytest

from app.core.ids import uuid7, uuid7_timestamp_ms


def test_uuid7_has_version_7_and_rfc_variant() -> None:
    value = uuid7()
    assert value.version == 7
    assert value.variant == uuid.RFC_4122


def test_uuid7_embeds_current_time() -> None:
    before = time.time_ns() // 1_000_000
    value = uuid7()
    after = time.time_ns() // 1_000_000
    assert before <= uuid7_timestamp_ms(value) <= after


def test_uuid7_orders_by_time() -> None:
    earlier = uuid7(timestamp_ms=1_700_000_000_000)
    later = uuid7(timestamp_ms=1_700_000_000_001)
    assert earlier < later


def test_uuid7_is_unique() -> None:
    values = {uuid7() for _ in range(10_000)}
    assert len(values) == 10_000


def test_uuid7_rejects_out_of_range_timestamp() -> None:
    with pytest.raises(ValueError):
        uuid7(timestamp_ms=1 << 48)


def test_timestamp_requires_v7() -> None:
    with pytest.raises(ValueError):
        uuid7_timestamp_ms(uuid.uuid4())


def test_uuid7_is_monotonic_within_one_millisecond() -> None:
    ids = [uuid7() for _ in range(5000)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)
