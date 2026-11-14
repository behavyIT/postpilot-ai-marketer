"""Генерация идентификаторов UUIDv7 (RFC 9562).

UUIDv7 упорядочены по времени создания: индексы по первичному ключу растут монотонно,
а сами идентификаторы безопасно показывать в callback_data Telegram — их нельзя перебрать.
В стандартной библиотеке uuid7 появился только в Python 3.14, поэтому реализуем сами.
"""

from __future__ import annotations

import os
import time
import uuid

_TIMESTAMP_BITS = 48
_RAND_A_BITS = 12
_RAND_B_BITS = 62


def uuid7(timestamp_ms: int | None = None) -> uuid.UUID:
    """Вернуть UUID версии 7: 48 бит времени в мс, версия, 74 случайных бита, вариант RFC."""
    ts = time.time_ns() // 1_000_000 if timestamp_ms is None else timestamp_ms
    if not 0 <= ts < (1 << _TIMESTAMP_BITS):
        raise ValueError("timestamp_ms вне диапазона 48 бит")

    random_bits = int.from_bytes(os.urandom(10), "big")
    rand_a = (random_bits >> _RAND_B_BITS) & ((1 << _RAND_A_BITS) - 1)
    rand_b = random_bits & ((1 << _RAND_B_BITS) - 1)

    value = (ts << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return uuid.UUID(int=value)


def uuid7_timestamp_ms(value: uuid.UUID) -> int:
    """Извлечь метку времени (мс) из UUIDv7."""
    if value.version != 7:
        raise ValueError("Ожидается UUID версии 7")
    return value.int >> 80
