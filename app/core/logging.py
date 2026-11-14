"""Структурное логирование (structlog) с маскированием секретов."""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

_SENSITIVE_KEY_PARTS = ("token", "secret", "password", "authorization", "auth_key", "api_key", "credentials")
_MASK = "***"


def redact_secrets(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Заменить значения полей, похожих на секреты, на маску."""
    for key in list(event_dict):
        if any(part in key.lower() for part in _SENSITIVE_KEY_PARTS):
            event_dict[key] = _MASK
    return event_dict


def configure_logging(level: str = "INFO", *, json: bool = False) -> None:
    """Настроить structlog и стандартный logging (для сторонних библиотек)."""
    numeric_level = logging.getLevelNamesMapping()[level.upper()]
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=numeric_level, force=True)

    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer(ensure_ascii=False) if json else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_secrets,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.typing.FilteringBoundLogger:
    """Получить логгер; имя модуля попадает в поле logger_name."""
    logger: structlog.typing.FilteringBoundLogger = (
        structlog.get_logger(logger_name=name) if name else structlog.get_logger()
    )
    return logger
