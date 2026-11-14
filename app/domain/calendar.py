"""Календарь праздников и маркетинговых дат РФ для контент-плана (статический, без внешних источников)."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    day: date
    title: str


_FIXED: dict[tuple[int, int], str] = {
    (1, 1): "Новый год",
    (1, 7): "Рождество",
    (1, 25): "Татьянин день — День студента",
    (2, 14): "День святого Валентина",
    (2, 23): "День защитника Отечества",
    (3, 8): "Международный женский день",
    (4, 1): "День смеха",
    (4, 12): "День космонавтики",
    (5, 1): "Праздник Весны и Труда",
    (5, 9): "День Победы",
    (6, 1): "День защиты детей",
    (6, 12): "День России",
    (7, 8): "День семьи, любви и верности",
    (9, 1): "День знаний",
    (10, 1): "Международный день кофе",
    (10, 5): "День учителя",
    (11, 4): "День народного единства",
    (12, 31): "Канун Нового года",
}


def _black_friday(year: int) -> date:
    """Четвёртая пятница ноября."""
    fridays = [d for d in range(1, 31) if calendar.weekday(year, 11, d) == calendar.FRIDAY]
    return date(year, 11, fridays[3])


def events_between(start: date, end: date) -> list[CalendarEvent]:
    events: list[CalendarEvent] = []
    day = start
    while day <= end:
        title = _FIXED.get((day.month, day.day))
        if title:
            events.append(CalendarEvent(day, title))
        if day == _black_friday(day.year):
            events.append(CalendarEvent(day, "Чёрная пятница"))
        day += timedelta(days=1)
    return events
