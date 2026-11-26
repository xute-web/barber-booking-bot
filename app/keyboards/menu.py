from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

BOOK = "Записаться"
MY_BOOKINGS = "Мои записи"
PORTFOLIO = "Посмотреть работы"
CANCEL = "Отмена"
ADMIN_PANEL = "Админ-панель"


def main_menu(*, is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=BOOK), KeyboardButton(text=MY_BOOKINGS)]]
    rows.append([KeyboardButton(text=PORTFOLIO)])
    if is_admin:
        rows.append([KeyboardButton(text=ADMIN_PANEL)])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
    )
