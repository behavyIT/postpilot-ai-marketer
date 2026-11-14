"""Технические ограничения площадок и проверка текста перед публикацией."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

from app.domain.enums import Platform


@dataclass(frozen=True, slots=True)
class PlatformSpec:
    platform: Platform
    title: str
    max_text: int  # пост без картинки
    max_caption: int  # подпись к картинке
    allowed_tags: frozenset[str]
    max_hashtags: int
    style_hint: str  # подсказка копирайтеру о формате площадки


TELEGRAM = PlatformSpec(
    platform=Platform.TELEGRAM,
    title="Telegram-канал",
    max_text=4096,
    max_caption=1024,
    allowed_tags=frozenset({"b", "strong", "i", "em", "u", "s", "a", "code", "blockquote"}),
    max_hashtags=5,
    style_hint=(
        "Telegram-канал: живая подача, короткие абзацы, допустимы эмодзи. "
        "Первая строка — цепляющий заголовок жирным. "
        "Разметка — только HTML-теги <b>, <i>, <u>, <s>, <a href>. "
        "Если к посту будет картинка, уложись в 1000 символов."
    ),
)
VK = PlatformSpec(
    platform=Platform.VK,
    title="Сообщество ВКонтакте",
    max_text=15_000,
    max_caption=15_000,
    allowed_tags=frozenset(),
    max_hashtags=7,
    style_hint=(
        "ВКонтакте: без HTML-разметки, обычный текст с абзацами и эмодзи, хештеги в конце. "
        "Можно призвать к обсуждению в комментариях."
    ),
)
MAX = PlatformSpec(
    platform=Platform.MAX,
    title="Канал в MAX",
    max_text=4000,
    max_caption=4000,
    allowed_tags=frozenset({"b", "strong", "i", "em", "u", "s", "a", "code"}),
    max_hashtags=5,
    style_hint=(
        "MAX: сдержанная подача, короткие абзацы, разметка — <b>, <i>, <a href>. Не больше 4000 символов."
    ),
)
SPECS = {spec.platform: spec for spec in (TELEGRAM, VK, MAX)}


class _TagCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.open: list[str] = []
        self.unknown: set[str] = set()
        self.unbalanced = False
        self.allowed: frozenset[str] = frozenset()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in self.allowed:
            self.unknown.add(tag)
        self.open.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if self.open and self.open[-1] == tag:
            self.open.pop()
        else:
            self.unbalanced = True


_TAG_RE = re.compile(r"<[^>]+>")


def visible_length(text: str) -> int:
    return len(_TAG_RE.sub("", text))


def strip_tags(text: str) -> str:
    return _TAG_RE.sub("", text)


def validate_for_platform(text: str, spec: PlatformSpec, *, with_image: bool) -> list[str]:
    """Нарушения технических ограничений площадки. Пустой список — текст можно публиковать."""
    problems: list[str] = []
    limit = spec.max_caption if with_image else spec.max_text
    length = visible_length(text)
    if length > limit:
        problems.append(f"Текст длиннее лимита {spec.title}: {length} из {limit} символов")
    if not text.strip():
        problems.append("Пустой текст")
    collector = _TagCollector()
    collector.allowed = spec.allowed_tags
    collector.feed(text)
    collector.close()
    if collector.unknown:
        problems.append("Недопустимая разметка: " + ", ".join(sorted(f"<{t}>" for t in collector.unknown)))
    if collector.unbalanced or collector.open:
        problems.append("Незакрытые HTML-теги")
    return problems
