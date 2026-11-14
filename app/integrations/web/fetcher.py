"""Загрузка страниц сайтов: лимиты размера и времени, ручные перенаправления."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from app.core.errors import AppError, DomainValidationError
from app.integrations.web.safe_transport import SafeTransport
from app.integrations.web.urls import normalize_site_url

USER_AGENT = "AIMarketerBot/0.1 (business profile research; respects robots.txt)"
ROBOTS_AGENT = "AIMarketerBot"


class FetchError(AppError):
    """Страницу не удалось загрузить (сеть, таймаут, слишком много перенаправлений)."""


@dataclass(frozen=True, slots=True)
class FetchResult:
    url: str
    status_code: int
    content_type: str
    body: bytes
    truncated: bool

    @property
    def is_html(self) -> bool:
        return self.content_type in {"text/html", "application/xhtml+xml"}


class SafeFetcher:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 15.0,
        max_bytes: int = 3 * 1024 * 1024,
        max_redirects: int = 5,
    ) -> None:
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._client = httpx.AsyncClient(
            transport=transport or SafeTransport(),
            timeout=httpx.Timeout(timeout_s, connect=5.0),
            follow_redirects=False,
            trust_env=False,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.8,*/*;q=0.5",
                "Accept-Language": "ru,en;q=0.7",
            },
        )

    async def get(self, url: str) -> FetchResult:
        current = normalize_site_url(url)
        for _ in range(self._max_redirects + 1):
            try:
                async with self._client.stream("GET", current) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise FetchError("Сайт вернул перенаправление без адреса")
                        # Новый адрес проходит те же проверки, что и исходный.
                        current = normalize_site_url(urljoin(current, location))
                        continue
                    body, truncated = await self._read_limited(response)
                    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                    return FetchResult(
                        url=str(response.url),
                        status_code=response.status_code,
                        content_type=content_type,
                        body=body,
                        truncated=truncated,
                    )
            except DomainValidationError:
                raise
            except httpx.TimeoutException as exc:
                raise FetchError("Сайт слишком долго не отвечает") from exc
            except httpx.HTTPError as exc:
                raise FetchError(f"Не удалось загрузить страницу: {type(exc).__name__}") from exc
        raise FetchError("Слишком много перенаправлений")

    async def _read_limited(self, response: httpx.Response) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)
            size += len(chunk)
            if size > self._max_bytes:
                return b"".join(chunks)[: self._max_bytes], True
        return b"".join(chunks), False

    async def aclose(self) -> None:
        await self._client.aclose()
