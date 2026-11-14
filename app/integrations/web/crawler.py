"""Обход сайта компании: robots.txt, приоритетные страницы, лимиты.

Не пытается обходить авторизацию или защиту: страницы с ошибками, капчей
или запретом в robots.txt просто пропускаются.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from app.core.errors import AppError
from app.core.logging import get_logger
from app.integrations.web.fetcher import ROBOTS_AGENT, SafeFetcher
from app.integrations.web.html import parse_html
from app.integrations.web.urls import normalize_site_url

log = get_logger(__name__)

_PRIORITY_WORDS = (
    "about", "o-nas", "o_nas", "onas", "o-kompanii", "company", "kompaniya", "uslugi", "services",
    "service", "catalog", "katalog", "menu", "menyu", "price", "prices", "tseny", "ceny", "prais",
    "contacts", "contact", "kontakty", "faq", "voprosy", "dostavka", "delivery", "oplata", "akcii",
    "products", "produkty", "tovary", "shop", "team", "komanda", "reviews", "otzyvy",
)  # fmt: skip
_SKIP_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".zip", ".rar", ".mp3", ".mp4", ".avi", ".css", ".js", ".xml", ".json", ".ico",
)  # fmt: skip


@dataclass(slots=True)
class CrawledPage:
    url: str
    title: str | None
    description: str | None
    text: str
    http_status: int
    content_hash: str
    structured_data: dict[str, Any]
    fetched_at: datetime


@dataclass(slots=True)
class CrawlResult:
    pages: list[CrawledPage] = field(default_factory=list)
    skipped_by_robots: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class SiteCrawler:
    def __init__(self, fetcher: SafeFetcher, *, max_pages: int = 12, delay_s: float = 0.3) -> None:
        self._fetcher = fetcher
        self._max_pages = max_pages
        self._delay_s = delay_s

    async def crawl(self, start_url: str) -> CrawlResult:
        root = normalize_site_url(start_url)
        host = _site_key(root)
        robots = await self._load_robots(root)
        result = CrawlResult()

        queue: list[tuple[int, str]] = [(-100, root)]
        seen = {root}
        while queue and len(result.pages) < self._max_pages:
            queue.sort()
            _, url = queue.pop(0)
            if robots is not None and not robots.can_fetch(ROBOTS_AGENT, url):
                result.skipped_by_robots.append(url)
                continue
            try:
                response = await self._fetcher.get(url)
            except AppError as exc:
                result.errors.append(f"{url}: {exc}")
                continue
            if response.status_code != 200 or not response.is_html:
                result.errors.append(f"{url}: HTTP {response.status_code} {response.content_type}")
                continue

            parsed = parse_html(response.body, response.url)
            if parsed.text:
                result.pages.append(
                    CrawledPage(
                        url=response.url,
                        title=parsed.title,
                        description=parsed.description,
                        text=parsed.text,
                        http_status=response.status_code,
                        content_hash=hashlib.sha256(parsed.text.encode()).hexdigest(),
                        structured_data=parsed.structured_data,
                        fetched_at=datetime.now(UTC),
                    )
                )
            for link, anchor in parsed.links:
                candidate = _same_site_url(link, host)
                if candidate and candidate not in seen:
                    seen.add(candidate)
                    queue.append((_priority(candidate, anchor), candidate))
            if self._delay_s:
                await asyncio.sleep(self._delay_s)

        result.pages = _strip_boilerplate(_dedupe(result.pages))
        log.info(
            "crawler.done",
            site=root,
            pages=len(result.pages),
            robots_skipped=len(result.skipped_by_robots),
            errors=len(result.errors),
        )
        return result

    async def _load_robots(self, root: str) -> RobotFileParser | None:
        parts = urlsplit(root)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        parser = RobotFileParser(robots_url)
        try:
            response = await self._fetcher.get(robots_url)
        except AppError:
            return None  # robots.txt недоступен — ограничений нет
        if response.status_code in (401, 403):
            parser.parse(
                ["User-agent: *", "Disallow: /"]
            )  # доступ к robots.txt закрыт — считаем, что запрещено всё
            return parser
        if response.status_code >= 400:
            return None
        parser.parse(response.body.decode("utf-8", errors="replace").splitlines())
        return parser


def _site_key(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


def _same_site_url(link: str, site_key: str) -> str | None:
    parts = urlsplit(link)
    if parts.scheme not in ("http", "https") or parts.query:
        return None  # ссылки с параметрами пропускаем: фильтры и пагинация плодят бесконечные адреса
    if (parts.hostname or "").lower().removeprefix("www.") != site_key:
        return None
    if parts.path.lower().endswith(_SKIP_EXTENSIONS):
        return None
    try:
        return normalize_site_url(link)
    except AppError:
        return None


def _priority(url: str, anchor: str) -> int:
    """Меньше — важнее. Страницы «о нас», услуги, цены и контакты идут первыми."""
    path = urlsplit(url).path.lower()
    depth = path.strip("/").count("/")
    text = f"{path} {anchor.lower()}"
    keywords_ru = (
        "о нас",
        "о компании",
        "услуг",
        "цен",
        "прайс",
        "контакт",
        "меню",
        "доставк",
        "вопрос",
        "каталог",
    )
    important = any(word in text for word in _PRIORITY_WORDS) or any(word in text for word in keywords_ru)
    return depth * 10 - (50 if important else 0)


def _dedupe(pages: list[CrawledPage]) -> list[CrawledPage]:
    seen: set[str] = set()
    unique = []
    for page in pages:
        if page.content_hash not in seen:
            seen.add(page.content_hash)
            unique.append(page)
    return unique


def _strip_boilerplate(pages: list[CrawledPage]) -> list[CrawledPage]:
    """Строки, которые повторяются на большинстве страниц (меню, подвал), оставляем только на первой."""
    if len(pages) < 3:
        return pages
    counts: Counter[str] = Counter()
    for page in pages:
        counts.update(set(page.text.splitlines()))
    threshold = max(2, int(len(pages) * 0.5))
    common = {line for line, count in counts.items() if count >= threshold}
    for page in pages[1:]:
        page.text = "\n".join(line for line in page.text.splitlines() if line not in common)
    return [pages[0], *(p for p in pages[1:] if p.text.strip())]
