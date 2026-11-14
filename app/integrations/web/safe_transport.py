"""HTTP-транспорт с защитой от SSRF на уровне соединения.

Проверка адреса перед запросом не защищает от DNS rebinding: между проверкой
и соединением имя может начать указывать на внутренний адрес. Поэтому проверка
встроена в сетевой бэкенд httpcore: имя разрешается здесь, все адреса должны быть
публичными, и соединение открывается к уже проверенному IP. TLS при этом
проверяет сертификат по исходному имени хоста (SNI). Проверка срабатывает
на каждом соединении, в том числе после перенаправлений.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
from collections.abc import Awaitable, Callable, Iterable

import certifi
import httpcore
import httpx

from app.core.errors import DomainValidationError

ALLOWED_PORTS = frozenset({80, 443})

Resolver = Callable[[str, int], Awaitable[list[str]]]


class BlockedAddressError(DomainValidationError):
    """Адрес ведёт во внутреннюю сеть или на запрещённый порт."""


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def is_public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value.split("%", 1)[0])  # отбрасываем zone id у IPv6
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


class SafeNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, resolver: Resolver = system_resolver) -> None:
        self._inner = httpcore.AnyIOBackend()
        self._resolve = resolver

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 — сигнатура задана интерфейсом httpcore
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if port not in ALLOWED_PORTS:
            raise BlockedAddressError(f"Порт {port} запрещён — разрешены только 80 и 443")
        try:
            addresses = await self._resolve(host, port)
        except OSError as exc:
            raise httpcore.ConnectError(f"Не удалось найти сайт {host}") from exc
        if not addresses:
            raise httpcore.ConnectError(f"Не удалось найти сайт {host}")
        blocked = [ip for ip in addresses if not is_public_ip(ip)]
        if blocked:
            raise BlockedAddressError(f"Сайт {host} указывает на внутренний адрес — загрузка запрещена")

        last_error: Exception | None = None
        for ip in addresses:
            try:
                return await self._inner.connect_tcp(
                    ip, port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except httpcore.ConnectError as exc:
                last_error = exc
        raise last_error or httpcore.ConnectError(f"Не удалось подключиться к {host}")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 — сигнатура задана интерфейсом httpcore
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise BlockedAddressError("Unix-сокеты запрещены")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class SafeTransport(httpx.AsyncHTTPTransport):
    """httpx-транспорт, который соединяется только с публичными адресами."""

    def __init__(self, resolver: Resolver = system_resolver) -> None:
        super().__init__(trust_env=False)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(cafile=certifi.where()),
            max_connections=10,
            max_keepalive_connections=5,
            keepalive_expiry=5.0,
            network_backend=SafeNetworkBackend(resolver),
        )
