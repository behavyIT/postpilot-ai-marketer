"""Конечный автомат статусов, ссылки на версии, разбор времени публикации."""

from datetime import datetime
from itertools import pairwise
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from app.agents.orchestrator.replies import Action, Button, Reply
from app.bot.keyboards import build_keyboard
from app.domain.enums import PublicationStatus as S
from app.domain.publications.hashing import content_hash
from app.domain.publications.refs import PostRef
from app.domain.publications.state_machine import (
    TRANSITIONS,
    InvalidTransitionError,
    can_transition,
    ensure_transition,
)
from app.domain.timeparse import parse_when

TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 11, 14, 15, 20, tzinfo=TZ)  # суббота


def test_every_status_is_in_the_table() -> None:
    assert set(TRANSITIONS) == set(S)


@pytest.mark.parametrize("status", [S.DRAFT, S.PENDING_APPROVAL, S.NEEDS_REVISION, S.REJECTED, S.APPROVED])
def test_only_scheduled_posts_can_start_publishing(status: S) -> None:
    assert not can_transition(status, S.PUBLISHING)
    assert not can_transition(status, S.PUBLISHED)


def test_publishing_path_and_final_states() -> None:
    path = [S.DRAFT, S.PENDING_APPROVAL, S.APPROVED, S.SCHEDULED, S.PUBLISHING, S.PUBLISHED]
    for current, target in pairwise(path):
        ensure_transition(current, target)
    assert TRANSITIONS[S.PUBLISHED] == frozenset()
    assert TRANSITIONS[S.CANCELLED] == frozenset()


def test_edit_of_approved_or_scheduled_post_goes_back_to_revision() -> None:
    assert can_transition(S.APPROVED, S.NEEDS_REVISION)
    assert can_transition(S.SCHEDULED, S.NEEDS_REVISION)
    assert not can_transition(S.NEEDS_REVISION, S.APPROVED)  # только через новое одобрение


def test_invalid_transition_has_readable_message() -> None:
    with pytest.raises(InvalidTransitionError, match="ждёт одобрения"):
        ensure_transition(S.PENDING_APPROVAL, S.SCHEDULED)


def test_content_hash_covers_text_and_image() -> None:
    base = content_hash("<b>Раф</b>", None)
    assert base == content_hash("<b>Раф</b>", None)
    assert base != content_hash("<b>Раф!</b>", None)
    assert base != content_hash("<b>Раф</b>", "a" * 64)


def test_post_ref_roundtrip_and_callback_limit() -> None:
    ref = PostRef(uuid4(), 12)
    assert PostRef.parse(ref.value()) == ref
    assert PostRef.parse(f"{ref.value()}.m10") == ref
    with pytest.raises(ValueError):
        PostRef.parse("испорчено")
    longest = [a for a in Action if a.value.startswith("post.")]
    reply = Reply(
        text="x", buttons=[[Button(text="b", action=a, value=f"{ref.value()}.custom")] for a in longest]
    )
    markup = build_keyboard(reply)
    assert markup is not None
    assert all(len((row[0].callback_data or "").encode()) <= 64 for row in markup.inline_keyboard)


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("Опубликуй завтра в 12:00", (2026, 11, 15, 12, 0)),
        ("сегодня в 18:30", (2026, 11, 14, 18, 30)),
        ("в 10", (2026, 11, 15, 10, 0)),  # 10:00 сегодня уже прошло
        ("в 6 вечера", (2026, 11, 14, 18, 0)),
        ("15.11 10:00", (2026, 11, 15, 10, 0)),
        ("20 ноября в 9", (2026, 11, 20, 9, 0)),
        ("10.01", (2027, 1, 10, 10, 0)),  # прошедшая дата — следующий год
        ("через 2 часа", (2026, 11, 14, 17, 20)),
        ("через полчаса", (2026, 11, 14, 15, 50)),
        ("в пятницу вечером", (2026, 11, 20, 19, 0)),
        ("в субботу в 10", (2026, 11, 21, 10, 0)),  # сегодня суббота, но 10:00 прошло
        ("послезавтра", (2026, 11, 16, 10, 0)),
        ("сейчас", (2026, 11, 14, 15, 20)),
    ],
)
def test_parse_when(phrase: str, expected: tuple[int, int, int, int, int]) -> None:
    assert parse_when(phrase, NOW) == datetime(*expected, tzinfo=TZ)


@pytest.mark.parametrize("phrase", ["когда-нибудь", "опубликуй", "35.13 99:99"])
def test_parse_when_returns_none_for_unclear_phrases(phrase: str) -> None:
    assert parse_when(phrase, NOW) is None
