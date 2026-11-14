from collections.abc import Callable

import httpx
import pytest

from app.core.errors import DomainValidationError
from app.integrations.web.crawler import SiteCrawler
from app.integrations.web.fetcher import FetchError, SafeFetcher
from app.integrations.web.html import parse_html, structured_to_text


def fetcher_for(handler: Callable[[httpx.Request], httpx.Response], **kwargs: int) -> SafeFetcher:
    return SafeFetcher(transport=httpx.MockTransport(handler), **kwargs)


def html(body: str, title: str = "Кофейня") -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/html; charset=utf-8"},
        content=f"<html><head><title>{title}</title></head><body>{body}</body></html>".encode(),
    )


async def test_redirect_to_internal_address_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})

    fetcher = fetcher_for(handler)
    with pytest.raises(DomainValidationError):
        await fetcher.get("https://coffee.example/")
    await fetcher.aclose()


async def test_too_many_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": f"{request.url}x"})

    fetcher = fetcher_for(handler, max_redirects=3)
    with pytest.raises(FetchError, match="перенаправлений"):
        await fetcher.get("https://coffee.example/")
    await fetcher.aclose()


async def test_large_body_is_truncated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"x" * 5000)

    fetcher = fetcher_for(handler, max_bytes=1000)
    result = await fetcher.get("https://coffee.example/")
    assert result.truncated is True
    assert len(result.body) == 1000
    assert result.is_html
    await fetcher.aclose()


async def test_timeout_becomes_friendly_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    fetcher = fetcher_for(handler)
    with pytest.raises(FetchError, match="долго"):
        await fetcher.get("https://coffee.example/")
    await fetcher.aclose()


def test_parse_html_extracts_text_meta_links_and_json_ld() -> None:
    page = parse_html(
        """<html><head><title> Кофейня «Зерно» </title>
        <meta name="description" content="Спешелти в центре Казани">
        <meta property="og:site_name" content="Зерно">
        <script type="application/ld+json">
        {"@type": "CafeOrCoffeeShop", "telephone": "+7 900 000-00-00"}
        </script>
        <script>var secret = 1;</script><style>.a{}</style></head>
        <body><h1>Меню</h1><p>Капучино — 250 ₽</p><a href="/contacts">Контакты</a>
        <a href="mailto:a@b.c">почта</a></body></html>""".encode(),
        "https://zerno.example/",
    )
    assert page.title == "Кофейня «Зерно»"
    assert page.description == "Спешелти в центре Казани"
    assert "Капучино — 250 ₽" in page.text
    assert "secret" not in page.text
    assert ("https://zerno.example/contacts", "Контакты") in page.links
    assert all(not link.startswith("mailto:") for link, _ in page.links)
    assert "+7 900 000-00-00" in structured_to_text(page.structured_data)
    assert "Название сайта: Зерно" in structured_to_text(page.structured_data)


def test_parse_html_detects_cp1251() -> None:
    body = '<html><head><meta charset="windows-1251"><title>Прайс</title></head><body>Цены</body></html>'
    page = parse_html(body.encode("cp1251"), "https://old.example/")
    assert page.title == "Прайс"
    assert "Цены" in page.text


async def test_crawler_respects_robots_priorities_and_limits() -> None:
    footer = "<footer>Тел. +7 900 000-00-00</footer>"
    pages = {
        "/": html(
            '<a href="/blog/news-1">Новость</a><a href="/contacts">Контакты</a>'
            '<a href="/private/admin">Админка</a><a href="/menu?sort=price">Сортировка</a>'
            '<a href="/logo.png">Лого</a><a href="https://other.example/">Чужой</a>'
            f"<p>Мы варим кофе</p>{footer}"
        ),
        "/contacts": html(f"<p>Казань, ул. Баумана, 1</p>{footer}", "Контакты"),
        "/blog/news-1": html(f"<p>Новости</p>{footer}", "Блог"),
        "/private/admin": html("<p>секрет</p>"),
    }
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
        return pages.get(request.url.path, httpx.Response(404))

    fetcher = fetcher_for(handler)
    result = await SiteCrawler(fetcher, max_pages=10, delay_s=0).crawl("https://zerno.example/")
    await fetcher.aclose()

    urls = [page.url for page in result.pages]
    assert urls[0] == "https://zerno.example/"
    assert urls.index("https://zerno.example/contacts") < urls.index("https://zerno.example/blog/news-1")
    assert "https://zerno.example/private/admin" in result.skipped_by_robots
    assert "/private/admin" not in requested
    assert not any(path.startswith(("/menu", "/logo")) for path in requested)
    # Подвал, повторяющийся на всех страницах, остаётся только на главной.
    assert "Тел. +7 900 000-00-00" in result.pages[0].text
    contacts = next(p for p in result.pages if p.url.endswith("/contacts"))
    assert "Тел." not in contacts.text
    assert "Баумана" in contacts.text


async def test_crawler_stops_at_max_pages() -> None:
    links = "".join(f'<a href="/p{i}">p{i}</a>' for i in range(20))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return html(f"{links}<p>Страница {request.url.path}</p>")

    fetcher = fetcher_for(handler)
    result = await SiteCrawler(fetcher, max_pages=4, delay_s=0).crawl("https://many.example/")
    await fetcher.aclose()
    assert len(result.pages) == 4
