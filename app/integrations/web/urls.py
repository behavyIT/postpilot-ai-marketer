"""Проверка и нормализация адресов сайтов, которые присылает пользователь.

Здесь — проверки, которые можно сделать без сети: схема, хост, порт, явные
внутренние адреса. Полная защита от SSRF (разрешение DNS, проверка каждого
редиректа, соединение по проверенному IP) выполняется при загрузке страниц (Этап 4).
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit, urlunsplit

from app.core.errors import DomainValidationError

_ALLOWED_PORTS = {None, 80, 443}
_BLOCKED_HOST_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".corp")


def normalize_site_url(raw: str) -> str:
    """Вернуть адрес вида https://example.com/path или бросить DomainValidationError."""
    candidate = raw.strip()
    if not candidate:
        raise DomainValidationError("Пустая ссылка")
    if "://" not in candidate:
        candidate = f"https://{candidate}"

    parts = urlsplit(candidate)
    if parts.scheme.lower() not in {"http", "https"}:
        raise DomainValidationError("Поддерживаются только ссылки http и https")
    if parts.username or parts.password:
        raise DomainValidationError("Ссылка не должна содержать логин или пароль")
    try:
        port = parts.port
    except ValueError as exc:
        raise DomainValidationError("В ссылке неверный порт") from exc
    if port not in _ALLOWED_PORTS:
        raise DomainValidationError("Поддерживаются только стандартные порты 80 и 443")

    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise DomainValidationError("В ссылке нет адреса сайта")
    _ensure_public_host(host)

    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise DomainValidationError("Некорректное доменное имя") from exc

    netloc = ascii_host if port is None else f"{ascii_host}:{port}"
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), netloc, path, parts.query, ""))


def _ensure_public_host(host: str) -> None:
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        if host == "localhost" or host.endswith(_BLOCKED_HOST_SUFFIXES) or "." not in host:
            raise DomainValidationError("Это внутренний адрес — нужна ссылка на публичный сайт") from None
        return
    if not ip.is_global:
        raise DomainValidationError("Это внутренний адрес — нужна ссылка на публичный сайт")
