"""Регистрация всех задач: воркер импортирует этот пакет."""

from app.workers.tasks import system

__all__ = ["system"]
