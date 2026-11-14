"""Время публикации из фразы владельца: «завтра в 10», «15.11 18:30», «через 2 часа», «в пятницу вечером».

Разбор детерминированный: время публикации — не то место, где можно позволить модели ошибиться.
Непонятная фраза → None, и бот переспрашивает.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

DEFAULT_TIME = time(10, 0)
_PARTS_OF_DAY = {"утром": time(10, 0), "днем": time(14, 0), "вечером": time(19, 0)}
_WEEKDAYS = {
    "понедельник": 0, "пн": 0,
    "вторник": 1, "вт": 1,
    "среду": 2, "среда": 2, "ср": 2,
    "четверг": 3, "чт": 3,
    "пятницу": 4, "пятница": 4, "пт": 4,
    "субботу": 5, "суббота": 5, "сб": 5,
    "воскресенье": 6, "вс": 6,
}  # fmt: skip
_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}  # fmt: skip

_NOW = re.compile(r"\b(сейчас|немедленно|сразу)\b")
_RELATIVE = re.compile(r"\bчерез\s+(?:(\d{1,3})\s*)?(полчаса|минут\w*|мин|час\w*|ч|дн\w*|день|недел\w*)?")
_TIME = re.compile(r"\b(\d{1,2})[:](\d{2})\b")
_HOUR = re.compile(r"\bв\s+(\d{1,2})(?![\d.:])(?:\s*(?:час\w*|ч\b))?(?:\s*(утра|дня|вечера|ночи))?")
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\b")
_WORD_DATE = re.compile(r"\b(\d{1,2})\s+(" + "|".join(_MONTHS) + r")\b")
_WEEKDAY = re.compile(r"\b(" + "|".join(sorted(_WEEKDAYS, key=len, reverse=True)) + r")\b")


def parse_when(text: str, now: datetime) -> datetime | None:
    """Момент публикации в часовом поясе now (now должен быть aware), или None."""
    t = " " + text.lower().replace("ё", "е").strip() + " "
    if _NOW.search(t):
        return now

    relative = _relative(t, now)
    if relative is not None:
        return relative

    day = _date(t, now)
    clock = _clock(t)
    if day is None and clock is None:
        return None
    if day is None:
        candidate = datetime.combine(now.date(), clock or DEFAULT_TIME, tzinfo=now.tzinfo)
        return candidate if candidate > now else candidate + timedelta(days=1)
    result = datetime.combine(day, clock or DEFAULT_TIME, tzinfo=now.tzinfo)
    if result <= now and _WEEKDAY.search(t) and not _has_explicit_date(t):
        result += timedelta(days=7)  # «в субботу в 10», а сегодня суббота 15:00 — значит, следующая
    return result


def _has_explicit_date(t: str) -> bool:
    return any(w in t for w in ("сегодня", "завтра")) or bool(_NUMERIC_DATE.search(t) or _WORD_DATE.search(t))


def _relative(t: str, now: datetime) -> datetime | None:
    match = _RELATIVE.search(t)
    if match is None or (match.group(1) is None and match.group(2) is None):
        return None
    amount = int(match.group(1)) if match.group(1) else 1
    unit = match.group(2) or "час"
    if unit == "полчаса":
        return now + timedelta(minutes=30)
    if unit.startswith("мин"):
        return now + timedelta(minutes=amount)
    if unit.startswith("ч"):
        return now + timedelta(hours=amount)
    if unit.startswith("недел"):
        return now + timedelta(weeks=amount)
    return now + timedelta(days=amount)


def _date(t: str, now: datetime) -> date | None:
    today = now.date()
    if "послезавтра" in t:
        return today + timedelta(days=2)
    if "завтра" in t:
        return today + timedelta(days=1)
    if "сегодня" in t:
        return today

    numeric = _NUMERIC_DATE.search(t)
    if numeric is not None:
        day_no, month = int(numeric.group(1)), int(numeric.group(2))
        year = int(numeric.group(3)) if numeric.group(3) else today.year
        if year < 100:
            year += 2000
        result = _safe_date(year, month, day_no)
        if result is not None:
            if numeric.group(3) is None and result < today:
                result = _safe_date(year + 1, month, day_no)
            return result

    worded = _WORD_DATE.search(t)
    if worded is not None:
        result = _safe_date(today.year, _MONTHS[worded.group(2)], int(worded.group(1)))
        if result is not None and result < today:
            result = _safe_date(today.year + 1, _MONTHS[worded.group(2)], int(worded.group(1)))
        return result

    weekday = _WEEKDAY.search(t)
    if weekday is not None:
        target = _WEEKDAYS[weekday.group(1)]
        ahead = (target - today.weekday()) % 7
        if ahead == 0 and "следующ" in t:
            ahead = 7
        return today + timedelta(days=ahead)
    return None


def _clock(t: str) -> time | None:
    exact = _TIME.search(t)
    if exact is not None:
        hour, minute = int(exact.group(1)), int(exact.group(2))
        return time(hour, minute) if hour < 24 and minute < 60 else None
    hour_match = _HOUR.search(t)
    if hour_match is not None:
        hour = int(hour_match.group(1))
        part = hour_match.group(2)
        if part in {"дня", "вечера"} and hour < 12:
            hour += 12
        elif part == "ночи" and hour == 12:
            hour = 0
        return time(hour, 0) if hour < 24 else None
    for word, value in _PARTS_OF_DAY.items():
        if word in t:
            return value
    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None
