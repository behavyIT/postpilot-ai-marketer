"""Состояния диалога (FSM): чего бот ждёт следующим сообщением."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class ProjectStates(StatesGroup):
    waiting_name = State()
