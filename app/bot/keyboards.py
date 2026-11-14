"""Inline-кнопки: Reply оркестратора → клавиатура Telegram."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.agents.orchestrator.replies import Reply

MAX_BUTTON_TEXT = 60


class ActionCallback(CallbackData, prefix="a"):
    """callback_data вида a:<действие>:<значение>; укладывается в лимит Telegram 64 байта."""

    action: str
    value: str = ""


def build_keyboard(reply: Reply) -> InlineKeyboardMarkup | None:
    if not reply.buttons:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=button.text[:MAX_BUTTON_TEXT],
                    callback_data=ActionCallback(action=button.action.value, value=button.value).pack(),
                )
                for button in row
            ]
            for row in reply.buttons
        ]
    )
