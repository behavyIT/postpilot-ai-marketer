from datetime import date

import pytest

from app.agents.compliance import check_claims
from app.domain.calendar import events_between
from app.domain.platforms import TELEGRAM, VK, validate_for_platform

FACTS = "Средний чек 350 ₽. Капучино 350/450/550 мл — 290/340/390 руб. Производство 100+ тонн в месяц"


def test_numbers_from_facts_pass_and_unknown_numbers_are_flagged() -> None:
    warnings = check_claims("Капучино за 290 ₽, раф за 250 ₽, скидка 20% студентам. 5 причин зайти!", FACTS)
    assert len(warnings) == 1
    assert "«250 ₽»" in warnings[0] and "«20%»" in warnings[0]
    assert "290" not in warnings[0]


def test_superlatives_and_promises_are_flagged() -> None:
    warnings = check_claims("Мы — лучшая кофейня, самый вкусный кофе. Гарантируем бодрость!", FACTS)
    assert any("закон" in w and "«лучшая»" in w and "«самый»" in w for w in warnings)
    assert any("«гарантируем»" in w for w in warnings)


def test_number_lists_and_comparatives_are_not_false_alarms() -> None:
    assert check_claims("Какая порция: 350, 450 или 550 мл? С ним день становится лучше.", FACTS) == []


def test_thousands_and_decimals_are_parsed_as_one_number() -> None:
    warnings = check_claims("Набор за 3 000 ₽, скидка 12,5%", FACTS)
    assert "«3 000 ₽»" in warnings[0] and "«12,5%»" in warnings[0]


def test_clean_text_has_no_warnings() -> None:
    assert check_claims("<b>Осенний раф</b>\nЗаходите к нам после пар — средний чек 350 ₽.", FACTS) == []


def test_platform_limits_and_markup() -> None:
    assert validate_for_platform("<b>Заголовок</b>\nТекст", TELEGRAM, with_image=True) == []
    assert any("лимита" in p for p in validate_for_platform("а" * 1100, TELEGRAM, with_image=True))
    assert validate_for_platform("а" * 1100, TELEGRAM, with_image=False) == []
    assert any("разметка" in p for p in validate_for_platform("<div>x</div>", TELEGRAM, with_image=False))
    assert any("Незакрытые" in p for p in validate_for_platform("<b>x<i>y</b>", TELEGRAM, with_image=False))
    assert any("разметка" in p for p in validate_for_platform("<b>x</b>", VK, with_image=False))


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (date(2026, 11, 20), date(2026, 11, 30), [(date(2026, 11, 27), "Чёрная пятница")]),
        (date(2026, 9, 30), date(2026, 10, 2), [(date(2026, 10, 1), "Международный день кофе")]),
        (date(2027, 1, 24), date(2027, 1, 26), [(date(2027, 1, 25), "Татьянин день — День студента")]),
    ],
)
def test_calendar_events(start: date, end: date, expected: list[tuple[date, str]]) -> None:
    assert [(e.day, e.title) for e in events_between(start, end)] == expected
