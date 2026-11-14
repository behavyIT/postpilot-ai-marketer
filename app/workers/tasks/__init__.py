"""Регистрация всех задач: воркер импортирует этот пакет."""

from app.workers.tasks import profile, system

__all__ = ["profile", "system"]
