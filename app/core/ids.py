"""Генерация идентификаторов UUIDv7 (RFC 9562).

UUIDv7 упорядочены по времени создания: индексы по первичному ключу растут монотонно,
а сами идентификаторы безопасно показывать в callback_data Telegram — их нельзя перебрать.
В стандартной библиотеке uuid7 появился только в Python 3.14, поэтому реализуем сами.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

_TIMESTAMP_BITS = 48
_RAND_A_BITS = 12
_RAND_B_BITS = 62


_lock = threading.Lock()
_last_ts = -1
_last_counter = 0


def uuid7(timestamp_ms: int | None = None) -> uuid.UUID:
    """Вернуть UUID версии 7: 48 бит времени в мс, версия, 74 случайных бита, вариант RFC.

    Без явной метки времени идентификаторы монотонны внутри процесса (RFC 9562, §6.2,
    метод 1): в пределах одной миллисекунды 12 бит rand_a работают как счётчик.
    Иначе записи, созданные в одну миллисекунду, сортировались бы случайно.
    """
    random_bits = int.from_bytes(os.urandom(10), "big")
    rand_b = random_bits & ((1 << _RAND_B_BITS) - 1)
    if timestamp_ms is not None:
        ts = timestamp_ms
        rand_a = (random_bits >> _RAND_B_BITS) & ((1 << _RAND_A_BITS) - 1)
    else:
        ts, rand_a = _next_timestamp_and_counter(random_bits >> _RAND_B_BITS)
    if not 0 <= ts < (1 << _TIMESTAMP_BITS):
        raise ValueError("timestamp_ms вне диапазона 48 бит")

    value = (ts << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return uuid.UUID(int=value)


def _next_timestamp_and_counter(seed: int) -> tuple[int, int]:
    global _last_ts, _last_counter
    with _lock:
        ts = time.time_ns() // 1_000_000
        if ts > _last_ts:
            counter = seed & 0x7FF  # старший бит счётчика свободен: запас на 2048 id в одну мс
        else:  # та же миллисекунда (или часы ушли назад) — продолжаем счётчик
            ts = _last_ts
            counter = _last_counter + 1
            if counter >= 1 << _RAND_A_BITS:
                ts, counter = ts + 1, 0
        _last_ts, _last_counter = ts, counter
        return ts, counter


def uuid7_timestamp_ms(value: uuid.UUID) -> int:
    """Извлечь метку времени (мс) из UUIDv7."""
    if value.version != 7:
        raise ValueError("Ожидается UUID версии 7")
    return value.int >> 80
