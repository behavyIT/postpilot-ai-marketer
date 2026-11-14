"""Регистрация всех задач: воркер импортирует этот пакет."""

from app.workers.tasks import content, profile, publish, system

__all__ = ["content", "profile", "publish", "system"]
