from collections.abc import Iterable
from datetime import datetime, timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.db.models import TimeSlot


def options(items: Iterable[tuple[str, str]]) -> InlineKeyboardMarkup:
    return button_rows([item] for item in items)


def button_rows(rows: Iterable[Iterable[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=value) for label, value in row]
        for row in rows
    ])


def slot_label(slot: TimeSlot, duration: int) -> str:
    end = datetime.combine(slot.date, slot.time) + timedelta(minutes=duration)
    return f"{slot.date:%d.%m.%Y}, {slot.time:%H:%M}–{end:%H:%M} (МСК)"
