"""Состояния диалога (FSM): чего бот ждёт следующим сообщением."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class ProjectStates(StatesGroup):
    waiting_name = State()


class PostStates(StatesGroup):
    """Данные состояния: ref — «<variant hex>.<версия>» поста, о котором идёт речь."""

    waiting_edit = State()  # что изменить в посте
    waiting_time = State()  # когда публиковать
