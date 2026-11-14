"""Разбор HTML: видимый текст, заголовок, описание, ссылки и структурированные данные.

Для профиля бизнеса нужен весь значимый текст страницы — услуги, цены, контакты
в подвале, — а не только «основная статья». Поэтому извлекается видимый текст
целиком, а повторяющиеся на всех страницах блоки (меню, подвал) убираются
позже, при сравнении страниц сайта между собой.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urldefrag, urljoin

from bs4 import BeautifulSoup, Tag

MAX_PAGE_TEXT = 20_000
_DROP_TAGS = ("script", "style", "noscript", "svg", "template", "iframe", "canvas", "head")
_SPACES = re.compile(r"[ \t ​]+")
_INTERESTING_LD_TYPES = {
    "organization",
    "localbusiness",
    "store",
    "restaurant",
    "cafeorcoffeeshop",
    "product",
    "offer",
    "service",
    "faqpage",
    "postaladdress",
    "contactpoint",
}


@dataclass(slots=True)
class ParsedPage:
    title: str | None
    description: str | None
    text: str
    links: list[tuple[str, str]] = field(default_factory=list)  # (абсолютный url, текст ссылки)
    structured_data: dict[str, Any] = field(default_factory=dict)


def parse_html(body: bytes, base_url: str) -> ParsedPage:
    soup = BeautifulSoup(body, "lxml")  # кодировку BeautifulSoup определит по meta charset

    title = _clean(soup.title.get_text()) if soup.title else None
    description = _meta(soup, "description") or _meta(soup, "og:description")
    structured = _structured_data(soup)
    og_site = _meta(soup, "og:site_name")
    if og_site:
        structured["og_site_name"] = og_site

    links: list[tuple[str, str]] = []
    for anchor in soup.find_all("a", href=True):
        if not isinstance(anchor, Tag):
            continue
        href = str(anchor.get("href", "")).strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute, _ = urldefrag(urljoin(base_url, href))
        links.append((absolute, _clean(anchor.get_text(" ")) or ""))

    for tag in soup(_DROP_TAGS):
        tag.decompose()
    lines = [_clean(line) for line in soup.get_text("\n").splitlines()]
    text = "\n".join(line for line in lines if line and len(line) > 1)
    return ParsedPage(
        title=title,
        description=description,
        text=text[:MAX_PAGE_TEXT],
        links=links,
        structured_data=structured,
    )


def _clean(value: str) -> str:
    return _SPACES.sub(" ", value).strip()


def _meta(soup: BeautifulSoup, name: str) -> str | None:
    tag = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
    if isinstance(tag, Tag):
        content = _clean(str(tag.get("content", "")))
        return content or None
    return None


def _structured_data(soup: BeautifulSoup) -> dict[str, Any]:
    """JSON-LD (schema.org): организация, адрес, товары, FAQ — надёжный источник фактов."""
    items: list[Any] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            payload = json.loads(script.get_text())
        except (ValueError, TypeError):
            continue
        for item in payload if isinstance(payload, list) else payload.get("@graph", [payload]):
            if isinstance(item, dict) and _is_interesting(item.get("@type")):
                items.append(item)
    return {"json_ld": items[:10]} if items else {}


def _is_interesting(value: Any) -> bool:
    types = value if isinstance(value, list) else [value]
    return any(isinstance(t, str) and t.lower() in _INTERESTING_LD_TYPES for t in types)


def structured_to_text(data: dict[str, Any]) -> str:
    """Компактное текстовое представление структурированных данных для модели."""
    parts: list[str] = []
    if data.get("og_site_name"):
        parts.append(f"Название сайта: {data['og_site_name']}")
    for item in data.get("json_ld", []):
        parts.append(json.dumps(item, ensure_ascii=False)[:2000])
    return "\n".join(parts)
