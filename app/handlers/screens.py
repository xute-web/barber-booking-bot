from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, Message


async def show_screen(message: Message, state: FSMContext, text: str,
                      markup: InlineKeyboardMarkup | None = None, *, move_below: bool = False) -> None:
    """Edit button-driven screens and move the prompt below typed replies."""
    data = await state.get_data()
    previous_id = data.get("screen_id")
    if previous_id is not None and not move_below:
        await message.bot.edit_message_text(
            chat_id=message.chat.id, message_id=previous_id, text=text, reply_markup=markup,
        )
    else:
        screen = await message.answer(text, reply_markup=markup)
        await state.update_data(screen_id=screen.message_id)
        if previous_id is not None:
            try:
                await message.bot.delete_message(chat_id=message.chat.id, message_id=previous_id)
            except TelegramBadRequest:
                pass
