"""Сборка роутеров. Порядок важен: команды раньше состояний FSM и свободного текста."""

from __future__ import annotations

from aiogram import Router

from app.bot.handlers import callbacks, commands, media, membership, projects, text


def build_router() -> Router:
    root = Router(name="root")
    root.include_routers(
        commands.router,
        projects.router,
        media.router,
        callbacks.router,
        membership.router,
        text.router,  # последним: ловит любой текст
    )
    return root
