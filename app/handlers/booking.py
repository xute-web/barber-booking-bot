from datetime import date
from pathlib import Path
from secrets import token_hex

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InputMediaPhoto, Message, User

from app.handlers.screens import show_screen
from app.keyboards.booking import button_rows, options, slot_label
from app.keyboards.menu import BOOK, CANCEL
from app.services.booking_service import BookingService, BookingUnavailable, ClientBusy, ServiceUnavailable, SlotUnavailable
from app.services.notifications import flush_outbox
from app.services.schedule_service import ScheduleService, booking_days, moscow_now
from app.utils.validators import normalize_name, normalize_phone

router = Router()
TIME_OPTIONS_PER_PAGE = 30


class Reservation(StatesGroup):
    service = State()
    master = State()
    day = State()
    slot = State()
    name = State()
    phone = State()
    confirm = State()


async def show_services(message: Message, state: FSMContext, booking: BookingService,
                        prompt: str = "Выберите услугу.") -> None:
    services = await booking.services()
    buttons = [
        (f"{item.name} · {item.duration_minutes} мин · {item.price} ₽", f"book:service:{item.id}")
        for item in services
    ]
    await show_screen(message, state, prompt, options([*buttons, (CANCEL, "book:cancel")]))
    await state.set_state(Reservation.service)


async def show_masters(message: Message, state: FSMContext, schedule: ScheduleService) -> None:
    data = await state.get_data()
    rows = [[(master.name, f"book:master:{master.id}")] for master in await schedule.masters()]
    rows.append([("Любой мастер", "book:master:0")])
    navigation = []
    if "reschedule_id" not in data:
        navigation.append(("‹ Назад", "book:services"))
    navigation.append((CANCEL, "book:cancel"))
    await show_screen(message, state, "Выберите мастера.", button_rows([*rows, navigation]))
    await state.set_state(Reservation.master)


async def show_days(message: Message, state: FSMContext,
                    prompt: str = "Выберите дату по московскому времени.") -> None:
    buttons = [(day.strftime("%d.%m.%Y"), f"book:day:{day.isoformat()}")
               for day in booking_days(moscow_now())]
    await show_screen(message, state, prompt, options([
        *buttons, ("Назад", "book:masters"), (CANCEL, "book:cancel"),
    ]))
    await state.set_state(Reservation.day)


@router.message(F.text == BOOK)
async def begin(message: Message, state: FSMContext, booking: BookingService) -> None:
    await state.clear()
    await show_services(message, state, booking)


@router.callback_query(F.data.regexp(r"^styles:(view|first|second|book):[0-9]+$"), F.message)
async def portfolio_card(query: CallbackQuery, state: FSMContext, schedule: ScheduleService,
                         booking: BookingService) -> None:
    _, action, raw_id = query.data.split(":")
    master = next((item for item in await schedule.masters() if item.id == int(raw_id)), None)
    if master is None:
        await query.answer("Мастер больше недоступен.", show_alert=True)
        return
    await query.answer()
    if action == "book":
        await state.clear()
        await state.update_data(portfolio_master=master.id)
        await show_services(query.message, state, booking, f"Мастер: {master.name}. Выберите услугу.")
        return
    styles = {1: ("Боковой пробор", "Зачёс назад"), 2: ("Текстурный кроп", "Высокий фейд"),
              3: ("Тейпер для кудрей", "Оформление бороды")}
    markup = options([("Записаться", f"styles:book:{master.id}")])
    if master.id in styles:
        second = action == "second"
        suffix = "-second" if second else ""
        photo = FSInputFile(Path(__file__).resolve().parents[1] / "assets" / f"styles-{master.id}{suffix}.png")
        caption = f"{master.name} · {styles[master.id][int(second)]}\nДемонстрационный пример · {2 if second else 1}/2"
        other = ("‹ Первое фото", f"styles:first:{master.id}") if second else ("Второе фото ›", f"styles:second:{master.id}")
        markup = options([other, ("Записаться", f"styles:book:{master.id}")])
        if action == "view":
            await query.message.answer_photo(photo, caption=caption, reply_markup=markup)
        else:
            await query.message.edit_media(InputMediaPhoto(media=photo, caption=caption), reply_markup=markup)
    else:
        await query.message.answer(f"Мастер: {master.name}", reply_markup=markup)


@router.callback_query(F.data == "book:manual", F.message)
async def manual(query: CallbackQuery, state: FSMContext, booking: BookingService, admin_id: int) -> None:
    if query.from_user.id != admin_id:
        await query.answer("Действие доступно только администратору.", show_alert=True)
        return
    await query.answer()
    await state.clear()
    await state.set_data({"manual": True, "screen_id": query.message.message_id})
    await show_services(query.message, state, booking, "Запись за клиента. Выберите услугу.")


@router.callback_query(F.data.regexp(r"^visit:move:[0-9]+:[0-9]+$"), F.message)
async def start_reschedule(query: CallbackQuery, state: FSMContext, booking: BookingService,
                           schedule: ScheduleService) -> None:
    booking_id, revision = map(int, query.data.split(":")[2:])
    details = await booking.upcoming(query.from_user.id, booking_id, revision)
    if details is None:
        await query.answer("Эта запись изменилась. Откройте «Мои записи».", show_alert=True)
        return
    record, service, slot, master = details
    await query.answer()
    await state.clear()
    await state.set_data({
        "reschedule_id": booking_id, "revision": revision, "old_slot_id": slot.id,
        "old_master_id": master.id, "service_name": service.name, "duration": record.duration_minutes,
    })
    await show_masters(query.message, state, schedule)


@router.callback_query(F.data.regexp(r"^visit:confirm:[0-9]+:[0-9]+$"))
async def confirm_visit(query: CallbackQuery, booking: BookingService) -> None:
    booking_id, revision = map(int, query.data.split(":")[2:])
    confirmed = await booking.confirm_visit(query.from_user.id, booking_id, revision)
    await query.answer(
        "Визит подтверждён." if confirmed else "Эта запись изменилась. Откройте «Мои записи».",
        show_alert=True,
    )


@router.callback_query(Reservation.service, F.data.regexp(r"^book:service:[0-9]+$"), F.message)
async def choose_service(query: CallbackQuery, state: FSMContext, booking: BookingService,
                         schedule: ScheduleService) -> None:
    service_id = int(query.data.rsplit(":", 1)[1])
    service = next((item for item in await booking.services() if item.id == service_id), None)
    if service is None:
        await query.answer("Услуга недоступна.", show_alert=True)
        return
    await query.answer()
    await state.update_data(service_id=service.id, service_name=service.name, price=service.price,
                            duration=service.duration_minutes)
    preferred = (await state.get_data()).get("portfolio_master")
    if preferred and any(master.id == preferred for master in await schedule.masters()):
        await state.update_data(master_id=preferred)
        await show_days(query.message, state)
        return
    await show_masters(query.message, state, schedule)


@router.callback_query(Reservation.master, F.data.regexp(r"^book:master:[0-9]+$"), F.message)
async def choose_master(query: CallbackQuery, state: FSMContext, schedule: ScheduleService) -> None:
    master_id = int(query.data.rsplit(":", 1)[1])
    if master_id and not any(item.id == master_id for item in await schedule.masters()):
        await query.answer("Мастер недоступен.", show_alert=True)
        return
    await query.answer()
    await state.update_data(master_id=master_id)
    await show_days(query.message, state)


@router.callback_query(Reservation.master, F.data == "book:services", F.message)
async def back_to_services(query: CallbackQuery, state: FSMContext, booking: BookingService) -> None:
    if "reschedule_id" in await state.get_data():
        await query.answer("Выберите мастера для переноса.", show_alert=True)
        return
    await query.answer()
    await show_services(query.message, state, booking)


@router.callback_query(Reservation.day, F.data == "book:masters", F.message)
async def back_to_masters(query: CallbackQuery, state: FSMContext, schedule: ScheduleService) -> None:
    await query.answer()
    await show_masters(query.message, state, schedule)


@router.callback_query(
    StateFilter(Reservation.day, Reservation.slot),
    F.data.regexp(r"^book:day:[0-9]{4}-[0-9]{2}-[0-9]{2}(?::[0-9]+)?$"), F.message,
)
async def choose_day(query: CallbackQuery, state: FSMContext, schedule: ScheduleService) -> None:
    _, _, raw_day, *page_number = query.data.split(":")
    try:
        day = date.fromisoformat(raw_day)
    except ValueError:
        await query.answer("Выберите дату из календаря.", show_alert=True)
        return
    await query.answer()
    data = await state.get_data()
    choices = await schedule.choices(day, data["master_id"], data["duration"],
                                     exclude_booking=data.get("reschedule_id"))
    buttons = []
    for slot, master in choices:
        if (slot.id, master.id) == (data.get("old_slot_id"), data.get("old_master_id")):
            continue
        when = slot.time.strftime("%H:%M")
        if data["master_id"]:
            buttons.append((when, f"book:slot:{slot.id}"))
        else:
            buttons.append((f"{when} · {master.name}", f"book:slot:{slot.id}:{master.id}"))
    prompt = f"Выберите время на {day:%d.%m.%Y}." if buttons else "На эту дату свободного времени нет."
    page = min(int(page_number[0]) if page_number else 0, max(0, (len(buttons) - 1) // TIME_OPTIONS_PER_PAGE))
    offset = page * TIME_OPTIONS_PER_PAGE
    page_buttons = buttons[offset:offset + TIME_OPTIONS_PER_PAGE]
    if page:
        page_buttons.append(("Предыдущие варианты", f"book:day:{raw_day}:{page - 1}"))
    if offset + TIME_OPTIONS_PER_PAGE < len(buttons):
        page_buttons.append(("Следующие варианты", f"book:day:{raw_day}:{page + 1}"))
    await show_screen(query.message, state, prompt, options([
        *page_buttons, ("Назад", "book:days"), (CANCEL, "book:cancel"),
    ]))
    await state.update_data(day=day.isoformat())
    await state.set_state(Reservation.slot)


@router.callback_query(StateFilter(Reservation.slot, Reservation.name, Reservation.phone, Reservation.confirm),
                       F.data == "book:days", F.message)
async def back_to_days(query: CallbackQuery, state: FSMContext) -> None:
    await query.answer()
    await show_days(query.message, state)


@router.callback_query(Reservation.slot, F.data.regexp(r"^book:slot:[0-9]+(?::[0-9]+)?$"), F.message)
async def choose_slot(query: CallbackQuery, state: FSMContext, schedule: ScheduleService) -> None:
    await query.answer()
    data = await state.get_data()
    slot_id, *master_ids = map(int, query.data.split(":")[2:])
    master_id = data["master_id"] or next(iter(master_ids), 0)
    choices = await schedule.choices(date.fromisoformat(data["day"]), master_id, data["duration"],
                                     exclude_booking=data.get("reschedule_id"))
    chosen = next((item for item in choices if item[0].id == slot_id and item[1].id == master_id), None)
    if chosen is None or (slot_id, master_id) == (data.get("old_slot_id"), data.get("old_master_id")):
        await show_days(query.message, state, "Это время уже недоступно. Выберите дату и другое время.")
        return
    slot, master = chosen
    when = slot_label(slot, data["duration"])
    await state.update_data(slot_id=slot.id, chosen_master_id=master.id, master_name=master.name, when=when)
    if "reschedule_id" in data:
        token = token_hex(8)
        await show_screen(query.message, state,
                          f"Новое время записи\n{data['service_name']}\nМастер: {master.name}\n{when}",
                          options([("Подтвердить перенос", f"book:confirm:{token}"),
                                   ("Назад", "book:days"), (CANCEL, "book:cancel")]))
        await state.update_data(confirmation=token)
        await state.set_state(Reservation.confirm)
        return
    prompt = "Как зовут клиента?" if data.get("manual") else "Как вас зовут?"
    await show_screen(query.message, state, prompt, options([
        ("Назад", "book:days"), (CANCEL, "book:cancel"),
    ]))
    await state.set_state(Reservation.name)


@router.message(Reservation.name)
async def enter_name(message: Message, state: FSMContext) -> None:
    name = normalize_name(message.text or "")
    if name is None:
        await show_screen(message, state, "Введите имя буквами. Например, Иван или Анна-Мария.", options([
            ("Назад", "book:days"), (CANCEL, "book:cancel"),
        ]), move_below=True)
        return
    await show_screen(message, state, "Введите телефон, например +7 900 123-45-67.", options([
        ("Назад", "book:days"), (CANCEL, "book:cancel"),
    ]), move_below=True)
    await state.update_data(full_name=name)
    await state.set_state(Reservation.phone)


@router.message(Reservation.phone)
async def enter_phone(message: Message, state: FSMContext) -> None:
    phone = normalize_phone(message.text or "")
    if phone is None:
        await message.answer("Введите российский номер телефона, например +7 900 123-45-67.")
        return
    confirmation = token_hex(8)
    data = await state.get_data()
    await show_screen(
        message, state,
        f"{data['service_name']} · {data['price']} ₽\nМастер: {data['master_name']}\n{data['when']}\n"
        f"{data['full_name']}\n{phone}",
        options([
            ("Подтвердить запись", f"book:confirm:{confirmation}"), ("Назад", "book:days"), (CANCEL, "book:cancel"),
        ]),
        move_below=True,
    )
    await state.update_data(phone=phone, confirmation=confirmation)
    await state.set_state(Reservation.confirm)


@router.callback_query(Reservation.confirm, F.data.startswith("book:confirm:"), F.message)
async def confirm(query: CallbackQuery, state: FSMContext, booking: BookingService, admin_id: int) -> None:
    data = await state.get_data()
    if query.data != f"book:confirm:{data['confirmation']}":
        await query.answer("Эта кнопка устарела. Подтвердите актуальную запись.", show_alert=True)
        return
    await query.answer()
    user: User = query.from_user
    manual = data.get("manual", False)
    if manual and user.id != admin_id:
        await state.clear()
        return
    moving = "reschedule_id" in data
    try:
        if moving:
            result, _, _ = await booking.reschedule(
                user.id, data["reschedule_id"], data["revision"], data["chosen_master_id"], data["slot_id"],
                notify_admin=admin_id,
            )
            record, service, slot, master = result
        else:
            record, service, slot, master = await booking.create(
                None if manual else user.id, None if manual else user.username,
                data["full_name"], data["phone"], data["service_id"], data["chosen_master_id"], data["slot_id"],
                notify_admin=admin_id,
            )
    except BookingUnavailable:
        await show_screen(query.message, state, "Запись изменилась. Откройте «Мои записи».")
        await state.clear()
        return
    except ClientBusy:
        await show_days(query.message, state, "У клиента уже есть запись на это время. Выберите другое время.")
        return
    except SlotUnavailable:
        await show_days(query.message, state, "Это время уже недоступно. Выберите дату и другое время.")
        return
    except ServiceUnavailable:
        await show_services(query.message, state, booking, "Эта услуга больше недоступна. Выберите другую.")
        return
    when = slot_label(slot, record.duration_minutes)
    details = f"{service.name}\nМастер: {master.name}\n{when}"
    contacts = f"Клиент: {record.full_name}\nТелефон: {record.phone}"
    cancel_button = ("Отменить запись", f"admin:notice_ask:{record.id}:{record.revision}")
    markup = None
    if moving:
        response = f"Запись перенесена.\n{details}"
    elif manual:
        response = f"Запись №{record.id} создана.\n{details}\n{contacts}"
        if record.user_id is None:
            response += "\nКлиент сможет получать напоминания после привязки номера через /phone."
        markup = options([cancel_button, ("В админ-панель", "admin:back")])
    else:
        response = f"Вы записаны на услугу «{service.name}».\nМастер: {master.name}\n{when}"
    await flush_outbox(query.bot, booking)
    try:
        await show_screen(query.message, state, response, markup)
    finally:
        await state.clear()


@router.callback_query(StateFilter(*Reservation.__all_states__), F.data == "book:cancel", F.message)
async def cancel_reservation(query: CallbackQuery, state: FSMContext) -> None:
    await query.answer()
    data = await state.get_data()
    if "reschedule_id" in data:
        await show_screen(query.message, state, "Перенос отменён. Прежняя запись сохранена.")
    elif data.get("manual"):
        await show_screen(query.message, state, "Оформление записи отменено.",
                          options([("В админ-панель", "admin:back")]))
    else:
        await show_screen(query.message, state, "Оформление записи отменено.")
    await state.clear()


@router.callback_query(F.data.startswith("book:"))
async def expired(query: CallbackQuery) -> None:
    await query.answer("Эта кнопка устарела. Начните с актуального меню.", show_alert=True)
