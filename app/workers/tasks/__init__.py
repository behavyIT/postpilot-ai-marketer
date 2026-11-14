"""Регистрация всех задач: воркер импортирует этот пакет."""

from app.workers.tasks import content, profile, system

__all__ = ["content", "profile", "system"]
