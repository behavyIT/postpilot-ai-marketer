"""Проверка текста публикации до согласования.

Правила детерминированные и объяснимые:
- цифры, цены и проценты в тексте должны встречаться в проверенных фактах о бизнесе —
  иначе это может быть выдумка модели;
- превосходные степени («лучший», «№1») и гарантии без подтверждения запрещены
  ФЗ «О рекламе» (ст. 5) — о них нужно предупредить владельца.
Предупреждения показываются владельцу; решение об публикации остаётся за ним.
"""

from __future__ import annotations

import re

from app.domain.platforms import strip_tags

# Число: 1 000 / 1 000 000 (разряды через пробел), 3,5 / 2.5 (дробь) или просто 350.
# «350, 450» — это два числа: после запятой с пробелом начинается новое.
_NUMBER_RE = re.compile(
    r"(?<![\w#])(\d{1,3}(?:[ \u00a0]\d{3})+(?!\d)|\d+(?:[.,]\d+)?)"
    r"(\s?(?:%|₽|руб\.?|рублей|рубля|р\.|тыс\.?|млн))?",
    re.IGNORECASE,
)
_SUPERLATIVE_RE = re.compile(
    # Только превосходная степень: «лучший», но не сравнительная «лучше».
    r"\b(лучш(?:ий|ая|ее|ие|его|ему|ую|им|ей|их|ими|ем)\b|самы[йеха]\w*|№\s?1|номер один"
    r"|единственн\w+|непревзойд\w+"
    r"|уникальн\w+ в (?:мире|россии|городе))",
    re.IGNORECASE,
)
_PROMISE_RE = re.compile(
    r"\b(гарантир\w+|100\s?%|навсегда|бесплатно навсегда|без риска|вылечи\w+)", re.IGNORECASE
)
_SMALL_NUMBERS = {str(n) for n in range(0, 11)}  # «3 причины», «5 ошибок» — не факты о бизнесе


def _norm_number(value: str) -> str:
    return re.sub(r"[\s.,]", "", value)


def check_claims(text: str, facts_text: str) -> list[str]:
    """Предупреждения о неподтверждённых цифрах, превосходных степенях и обещаниях."""
    plain = strip_tags(text)
    known = {_norm_number(m.group(1)) for m in _NUMBER_RE.finditer(facts_text)}
    warnings: list[str] = []

    unknown_numbers: list[str] = []
    for match in _NUMBER_RE.finditer(plain):
        raw = match.group(0).strip()
        number = _norm_number(match.group(1))
        has_unit = bool(match.group(2))
        if number in known or (number in _SMALL_NUMBERS and not has_unit):
            continue
        if raw not in unknown_numbers:
            unknown_numbers.append(raw)
    if unknown_numbers:
        warnings.append(
            "Проверьте цифры — их нет в профиле бизнеса: " + ", ".join(f"«{n}»" for n in unknown_numbers[:5])
        )

    superlatives = sorted({m.group(0).lower() for m in _SUPERLATIVE_RE.finditer(plain)})
    if superlatives:
        warnings.append(
            "Превосходная степень без подтверждения запрещена законом о рекламе: "
            + ", ".join(f"«{s}»" for s in superlatives[:5])
        )
    promises = sorted({m.group(0).lower() for m in _PROMISE_RE.finditer(plain)})
    if promises:
        warnings.append("Обещания, которые придётся выполнять: " + ", ".join(f"«{p}»" for p in promises[:5]))
    return warnings
