"""Конечный автомат статусов варианта публикации (§4.9 технического документа).

Все допустимые переходы — в одной таблице. Любой другой переход — исключение:
так «черновик» или «ждёт одобрения» физически не может попасть в публикацию.
"""

from __future__ import annotations

from app.core.errors import DomainValidationError
from app.domain.enums import PublicationStatus as S

TRANSITIONS: dict[S, frozenset[S]] = {
    S.DRAFT: frozenset({S.PENDING_APPROVAL, S.CANCELLED}),
    S.PENDING_APPROVAL: frozenset({S.APPROVED, S.NEEDS_REVISION, S.REJECTED, S.CANCELLED}),
    S.NEEDS_REVISION: frozenset({S.PENDING_APPROVAL, S.CANCELLED}),
    S.REJECTED: frozenset({S.NEEDS_REVISION}),
    S.APPROVED: frozenset({S.SCHEDULED, S.NEEDS_REVISION, S.CANCELLED}),
    S.SCHEDULED: frozenset({S.APPROVED, S.NEEDS_REVISION, S.CANCELLED, S.PUBLISHING}),
    S.PUBLISHING: frozenset({S.PUBLISHED, S.SCHEDULED, S.FAILED}),
    # FAILED → PUBLISHED: владелец подтвердил, что пост всё-таки вышел (исход был неизвестен).
    S.FAILED: frozenset({S.SCHEDULED, S.CANCELLED, S.PUBLISHED}),
    S.PUBLISHED: frozenset(),
    S.CANCELLED: frozenset(),
}

# Из этих статусов правка текста или картинки разрешена (через NEEDS_REVISION).
EDITABLE: frozenset[S] = frozenset(
    {S.DRAFT, S.PENDING_APPROVAL, S.NEEDS_REVISION, S.REJECTED, S.APPROVED, S.SCHEDULED}
)

LABELS: dict[S, str] = {
    S.DRAFT: "черновик",
    S.PENDING_APPROVAL: "⏳ ждёт одобрения",
    S.NEEDS_REVISION: "✏️ на доработке",
    S.APPROVED: "✅ одобрен",
    S.SCHEDULED: "📅 запланирован",
    S.PUBLISHING: "🚀 публикуется…",
    S.PUBLISHED: "📢 опубликован",
    S.REJECTED: "❌ отклонён",
    S.CANCELLED: "🚫 отменён",
    S.FAILED: "⚠️ не опубликован",
}


class InvalidTransitionError(DomainValidationError):
    def __init__(self, current: S, target: S) -> None:
        super().__init__(f"Нельзя перевести пост из статуса «{LABELS[current]}» в «{LABELS[target]}»")
        self.current = current
        self.target = target


def can_transition(current: S | str, target: S | str) -> bool:
    return S(target) in TRANSITIONS[S(current)]


def ensure_transition(current: S | str, target: S | str) -> None:
    if not can_transition(current, target):
        raise InvalidTransitionError(S(current), S(target))
