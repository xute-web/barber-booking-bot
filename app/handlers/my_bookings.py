from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.handlers.screens import show_screen
from app.keyboards.booking import button_rows, options, slot_label
from app.keyboards.menu import BOOK, MY_BOOKINGS
from app.services.booking_service import BookingDetails, BookingService
from app.services.notifications import flush_outbox

router = Router()


class ClientCancellation(StatesGroup):
    reason = State()
    confirmation = State()


def card_text(details: BookingDetails) -> str:
    record, service, slot, master = details
    return (
        f"{service.name} · {service.price} ₽\nМастер: {master.name}\n"
        f"{slot_label(slot, record.duration_minutes)}"
        + ("\nВизит подтверждён" if record.visit_confirmed else "")
    )


def card_buttons(details: BookingDetails, index: int, total: int) -> InlineKeyboardMarkup:
    record = details[0]
    buttons = [
        [("Перенести", f"visit:move:{record.id}:{record.revision}")],
        [("Отменить запись", f"mine:ask:{record.id}:{record.revision}")],
    ]
    navigation = []
    if index:
        navigation.append(("‹ Назад", f"mine:page:{index - 1}"))
    if index + 1 < total:
        navigation.append(("Дальше ›", f"mine:page:{index + 1}"))
    if navigation:
        buttons.append(navigation)
    return button_rows(buttons)


def visit_ids(callback_data: str) -> tuple[int, int]:
    _, _, booking_id, *revision = callback_data.split(":")
    return int(booking_id), int(revision[0]) if revision else 0


async def render_bookings(message: Message, user_id: int, booking: BookingService, index: int = 0,
                          *, selected_id: int | None = None, move_below: bool = False) -> None:
    records = await booking.for_client(user_id)
    if records:
        index = min(index, len(records) - 1)
        if selected_id is not None:
            index = next((i for i, item in enumerate(records) if item[0].id == selected_id), index)
        text = f"Мои записи · {index + 1} из {len(records)}\n\n{card_text(records[index])}"
        markup = card_buttons(records[index], index, len(records))
    else:
        text, markup = "У вас нет предстоящих записей.", None
    previous_id = await booking.screen_id(user_id)
    if previous_id is not None and not move_below:
        try:
            await message.bot.edit_message_text(chat_id=message.chat.id, message_id=previous_id,
                                                text=text, reply_markup=markup)
            return
        except TelegramBadRequest as error:
            if "message is not modified" in str(error).lower():
                return
            if "message to edit not found" not in str(error).lower():
                raise
    screen = await message.answer(text, reply_markup=markup)
    await booking.save_screen(user_id, screen.message_id)
    if previous_id is not None and move_below:
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=previous_id)
        except TelegramBadRequest as error:
            if "message can't be deleted" in str(error).lower():
                await message.bot.edit_message_text(chat_id=message.chat.id, message_id=previous_id,
                                                    text="Список записей открыт ниже.")
            elif "message to delete not found" not in str(error).lower():
                raise


@router.message(F.text == MY_BOOKINGS)
async def my_bookings(message: Message, state: FSMContext, booking: BookingService) -> None:
    await state.clear()
    await render_bookings(message, message.from_user.id, booking, move_below=True)


@router.callback_query(F.data.regexp(r"^mine:page:[0-9]+$"), F.message)
async def page(query: CallbackQuery, state: FSMContext, booking: BookingService) -> None:
    if await booking.screen_id(query.from_user.id) != query.message.message_id:
        await query.answer("Откройте актуальный список «Мои записи».", show_alert=True)
        return
    await query.answer()
    await state.clear()
    await render_bookings(query.message, query.from_user.id, booking, int(query.data.rsplit(":", 1)[1]))


@router.callback_query(F.data.regexp(r"^mine:ask:[0-9]+(?::[0-9]+)?$"), F.message)
async def ask_cancel(query: CallbackQuery, booking: BookingService, state: FSMContext) -> None:
    booking_id, revision = visit_ids(query.data)
    screen_id = await booking.screen_id(query.from_user.id)
    if screen_id is not None and screen_id != query.message.message_id:
        await query.answer("Откройте актуальный список «Мои записи».", show_alert=True)
        return
    if await booking.upcoming(query.from_user.id, booking_id, revision) is None:
        await query.answer("Запись больше недоступна.", show_alert=True)
        return
    await query.answer()
    await query.message.edit_text("Хотите указать причину отмены? Напишите её сообщением или отмените без причины.", reply_markup=options([
        ("Отменить без причины", f"mine:cancel:{booking_id}:{revision}"),
        ("Назад", f"mine:back:{booking_id}:{revision}"),
    ]))
    await state.set_data({"screen_id": query.message.message_id, "booking_id": booking_id, "revision": revision})
    await booking.save_screen(query.from_user.id, query.message.message_id)
    await state.set_state(ClientCancellation.reason)


@router.message(ClientCancellation.reason, ~F.text.in_({BOOK, MY_BOOKINGS}))
async def enter_reason(message: Message, state: FSMContext, booking: BookingService) -> None:
    data = await state.get_data()
    visit = f"{data['booking_id']}:{data['revision']}"
    reason = (message.text or "").strip()
    if not 1 <= len(reason) <= 500:
        await show_screen(message, state, "Введите причину текстом, до 500 символов.", options([
            ("Отменить без причины", f"mine:cancel:{visit}"), ("Назад", f"mine:back:{visit}"),
        ]), move_below=True)
    else:
        await show_screen(message, state, f"Причина отмены: {reason}", options([
            ("Подтвердить отмену", f"mine:cancel:{visit}"), ("Назад", f"mine:back:{visit}"),
        ]), move_below=True)
        await state.update_data(reason=reason)
        await state.set_state(ClientCancellation.confirmation)
    await booking.save_screen(message.from_user.id, (await state.get_data())["screen_id"])


@router.callback_query(F.data.regexp(r"^mine:back:[0-9]+(?::[0-9]+)?$"), F.message)
async def back(query: CallbackQuery, booking: BookingService, state: FSMContext) -> None:
    booking_id, revision = visit_ids(query.data)
    screen_id = await booking.screen_id(query.from_user.id)
    if screen_id is not None and screen_id != query.message.message_id:
        await query.answer("Откройте актуальный список «Мои записи».", show_alert=True)
        return
    details = await booking.upcoming(query.from_user.id, booking_id, revision)
    if details is None:
        await query.answer("Запись больше недоступна.", show_alert=True)
        return
    await query.answer()
    await render_bookings(query.message, query.from_user.id, booking, selected_id=booking_id)
    if (await state.get_data()).get("screen_id") == query.message.message_id:
        await state.clear()


@router.callback_query(F.data.regexp(r"^mine:cancel:[0-9]+(?::[0-9]+)?$"), F.message)
async def cancel(query: CallbackQuery, booking: BookingService, admin_id: int, state: FSMContext) -> None:
    booking_id, revision = visit_ids(query.data)
    user_id = query.from_user.id
    screen_id = await booking.screen_id(user_id)
    if screen_id is not None and screen_id != query.message.message_id:
        await query.answer("Откройте актуальный список «Мои записи».", show_alert=True)
        return
    data = await state.get_data()
    is_current = (data.get("screen_id"), data.get("booking_id"), data.get("revision")) == (
        query.message.message_id, booking_id, revision,
    )
    details = await booking.cancel(
        booking_id, user_id=user_id, revision=revision,
        notify_admin=admin_id, reason=data.get("reason") if is_current else None,
    )
    if details is None:
        await query.answer("Запись больше недоступна.", show_alert=True)
        return
    if is_current:
        await state.clear()
    await query.answer("Запись отменена.")
    if await booking.for_client(user_id):
        await render_bookings(query.message, user_id, booking)
    else:
        await booking.save_screen(user_id, None)
        try:
            await query.bot.delete_message(chat_id=query.message.chat.id, message_id=query.message.message_id)
        except TelegramBadRequest as error:
            if "message to delete not found" not in str(error).lower():
                await query.message.edit_text("Запись отменена.")
    await flush_outbox(query.bot, booking)
