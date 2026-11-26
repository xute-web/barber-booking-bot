import re
from datetime import date

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.exc import IntegrityError

from app.handlers.screens import show_screen
from app.keyboards.booking import button_rows, options, slot_label
from app.keyboards.menu import ADMIN_PANEL, BOOK, MY_BOOKINGS
from app.services.booking_service import BookingService
from app.services.notifications import flush_outbox
from app.services.schedule_service import (
    STEP_MINUTES, ScheduleService, Shift, booking_days, fits, minutes, moscow_now, overlaps,
)
from app.utils.validators import normalize_name

router = Router()


class AdminForm(StatesGroup):
    service = State()
    price = State()
    duration = State()
    cancellation_reason = State()
    cancellation_confirm = State()
    shift = State()
    master_name = State()
    master_confirm = State()


REASON_LIMIT = 500
WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
SHIFT_PROMPT = ("Введите смену и дополнительные перерывы по Москве, например 10:00–19:00 | 17:00–17:30. "
                "Обед с 14:00 до 15:00 действует автоматически.")


def panel() -> InlineKeyboardMarkup:
    return button_rows([
        [("Записи по датам", "admin:visits"), ("Записать клиента", "book:manual")],
        [("Графики мастеров", "admin:shifts"), ("Свободные слоты", "admin:slots")],
        [("Мастера", "admin:masters"), ("Управление услугами", "admin:services")],
    ])


def time_label(start: int) -> str:
    return f"{start // 60:02d}:{start % 60:02d}"


def parse_shift(text: str) -> Shift:
    def interval(value: str) -> list[int]:
        match = re.fullmatch(r"\s*(\d{1,2}):([0-5]\d)\s*[-–—]\s*(\d{1,2}):([0-5]\d)\s*", value)
        if match is None:
            raise ValueError
        first_hour, first_minute, last_hour, last_minute = map(int, match.groups())
        if first_hour >= 24 or last_hour >= 24:
            raise ValueError
        return [first_hour * 60 + first_minute, last_hour * 60 + last_minute]

    if text.strip().casefold() == "выходной":
        return None, None, []
    hours, separator, breaks_text = text.partition("|")
    start, end = interval(hours)
    breaks = sorted(interval(item) for item in breaks_text.split(",")) if separator else []
    return start, end, breaks


@router.message(Command("admin"))
@router.message(F.text == ADMIN_PANEL)
async def admin(message: Message, state: FSMContext, admin_id: int) -> None:
    if message.from_user.id != admin_id:
        await message.answer("Команда доступна только администратору.")
        return
    await state.clear()
    await message.answer("Панель администратора", reply_markup=panel())


@router.callback_query(F.data.startswith("admin:"), F.message)
async def action(query: CallbackQuery, state: FSMContext, admin_id: int,
                 booking: BookingService, schedule: ScheduleService) -> None:
    if query.from_user.id != admin_id:
        await query.answer("Действие доступно только администратору.", show_alert=True)
        return
    if query.data == "admin:occupied":
        await query.answer("Сначала отмените запись, затем заблокируйте слот", show_alert=True)
        return
    message = query.message
    data = await state.get_data()
    _, action_name, *args = query.data.split(":")
    if action_name == "notice_back":
        if data.get("screen_id") == message.message_id:
            await state.clear()
            await query.answer()
            await message.delete()
        else:
            await query.answer("Это меню уже закрыто.")
        return
    if action_name == "save_master":
        if await state.get_state() != AdminForm.master_confirm.state or data.get("screen_id") != message.message_id:
            await query.answer("Откройте актуальное подтверждение.", show_alert=True)
            return
        await query.answer()
        await state.set_state(None)
        if "remove_master_id" in data:
            removed = await schedule.remove_master(data["remove_master_id"])
            status = "Мастер удалён из новой записи. Существующие записи сохранены." if removed else "Мастер уже удалён."
        else:
            try:
                await schedule.add_master(data["new_master_name"])
            except ValueError as error:
                status = str(error)
            else:
                status = f"Мастер «{data['new_master_name']}» добавлен."
        await message.edit_text(status, reply_markup=options([("К списку мастеров", "admin:masters")]))
        return
    if action_name == "cancel":
        if (len(args) != 1 or args[0] != str(data.get("cancellation_id"))
                or data.get("screen_id") != message.message_id
                or await state.get_state() not in {
                    AdminForm.cancellation_reason.state, AdminForm.cancellation_confirm.state,
                }):
            await query.answer("Откройте актуальную карточку записи.", show_alert=True)
            return
        await query.answer()
        details = await booking.cancel(
            data["cancellation_id"], revision=data["cancellation_revision"],
            notify_client=True, reason=data.get("cancellation_reason"),
        )
        await state.set_state(None)
        back = data.get("admin_return", "admin:back")
        if details is None:
            await message.edit_text("Запись уже недоступна.", reply_markup=options([("Назад", back)]))
            return
        record = details[0]
        await flush_outbox(query.bot, booking)
        status = ("Запись отменена." if record.user_id is not None
                  else "Запись отменена. У клиента нет привязанного Telegram.")
        done = ("Закрыть", back) if back == "admin:notice_back" else ("Назад", back)
        await message.edit_text(status, reply_markup=options([done]))
        return
    await query.answer()
    await state.set_state(None)
    await state.update_data(screen_id=message.message_id)
    back = data.get("admin_return", "admin:back")
    if action_name in {"visits", "today"}:
        dates = [
            (f"{WEEKDAYS[day.weekday()]} {day:%d.%m}", f"admin:visits_day:{day.isoformat()}:0")
            for day in booking_days(moscow_now())
        ]
        await message.edit_text("📅 Записи по датам\nВыберите день.", reply_markup=button_rows([
            *[dates[index:index + 3] for index in range(0, len(dates), 3)],
            [("‹ В админ-панель", "admin:back")],
        ]))
    elif action_name == "visits_day" and len(args) == 2 and args[1].isdigit():
        try:
            day = date.fromisoformat(args[0])
        except ValueError:
            await message.edit_text("Выберите дату из календаря.",
                                    reply_markup=options([("К выбору даты", "admin:visits")]))
            return
        records = await booking.on_day(day)
        if not records:
            await message.edit_text(f"На {day:%d.%m.%Y} записей нет.",
                                    reply_markup=options([("К выбору даты", "admin:visits")]))
            return
        index = min(int(args[1]), len(records) - 1)
        record, service, slot, master = records[index]
        await state.update_data(admin_return=f"admin:visits_day:{day.isoformat()}:{index}")
        navigation = []
        if index > 0:
            navigation.append(("‹ Предыдущая", f"admin:visits_day:{day.isoformat()}:{index - 1}"))
        if index + 1 < len(records):
            navigation.append(("Следующая ›", f"admin:visits_day:{day.isoformat()}:{index + 1}"))
        start = index // 6 * 6
        overview = []
        for position, (visit, _, visit_slot, visit_master) in enumerate(records[start:start + 6], start):
            marker = "▸" if position == index else "·"
            overview.append(f"{marker} {visit_slot.time:%H:%M} · {visit_master.name} · {visit.full_name}")
        overview_text = "\n".join(overview)
        rows = [[("Отменить запись", f"admin:ask:{record.id}")]]
        quick = [
            (f"{visit_slot.time:%H:%M} · {visit_master.name}", f"admin:visits_day:{day.isoformat()}:{position}")
            for position, (_, _, visit_slot, visit_master) in enumerate(records[start:start + 6], start)
            if position != index
        ]
        rows.extend(quick[i:i + 2] for i in range(0, len(quick), 2))
        if navigation:
            rows.append(navigation)
        rows.append([("📅 К выбору даты", "admin:visits")])
        visit_status = "Запись по телефону · Telegram не привязан"
        if record.user_id is not None:
            visit_status = "Визит подтверждён" if record.visit_confirmed else "Ожидает подтверждения"
        await message.edit_text(
            f"📅 {WEEKDAYS[day.weekday()]} · {day:%d.%m.%Y} · {index + 1} из {len(records)}\n\n"
            f"{overview_text}\n\n"
            f"Запись №{record.id} · {slot_label(slot, record.duration_minutes)}\n"
            f"Услуга: {service.name}\nМастер: {master.name}\n"
            f"Клиент: {record.full_name}\nТелефон: {record.phone}\n"
            f"Статус: {visit_status}",
            reply_markup=button_rows(rows),
        )
    elif action_name == "masters":
        await message.edit_text("Управление мастерами", reply_markup=options([
            ("Добавить мастера", "admin:new_master"),
            *[(f"Удалить · {master.name}", f"admin:remove_master:{master.id}") for master in await schedule.masters()],
            ("Назад", "admin:back"),
        ]))
    elif action_name == "new_master":
        await state.set_data({"screen_id": message.message_id})
        await message.edit_text("Введите имя мастера.", reply_markup=options([("Назад", "admin:masters")]))
        await state.set_state(AdminForm.master_name)
    elif action_name == "remove_master" and len(args) == 1 and args[0].isdigit():
        master = next((item for item in await schedule.masters() if item.id == int(args[0])), None)
        if master is None:
            await message.edit_text("Мастер недоступен.", reply_markup=options([("Назад", "admin:masters")]))
            return
        await message.edit_text(f"Удалить мастера «{master.name}» из новой записи? Существующие визиты сохранятся.",
                                reply_markup=options([("Подтвердить удаление", "admin:save_master"),
                                                      ("Назад", "admin:masters")]))
        await state.set_data({"screen_id": message.message_id, "remove_master_id": master.id})
        await state.set_state(AdminForm.master_confirm)
    elif action_name == "services":
        services = await booking.services()
        await message.edit_text("Управление услугами. Выберите услугу для удаления или добавьте новую.",
                                reply_markup=options([
                                    ("Добавить услугу", "admin:add"),
                                    *[(f"{s.name} · {s.price} ₽ · {s.duration_minutes} мин", f"admin:hide:{s.id}") for s in services],
                                    ("Назад", "admin:back"),
                                ]))
    elif action_name in {"hide", "remove_service"} and len(args) == 1 and args[0].isdigit():
        service = next((s for s in await booking.services() if s.id == int(args[0])), None)
        back_button = ("К услугам", "admin:services")
        if service is None:
            await message.edit_text("Услуга уже недоступна.", reply_markup=options([back_button]))
        elif action_name == "hide":
            await message.edit_text(f"Удалить «{service.name}» из каталога? Оформленные визиты сохранятся.",
                                    reply_markup=options([("Подтвердить удаление", f"admin:remove_service:{service.id}"), back_button]))
        else:
            await booking.hide_service(service.id)
            await message.edit_text("Услуга удалена из каталога.", reply_markup=options([back_button]))
    elif action_name == "add":
        await state.set_state(AdminForm.service)
        await message.edit_text("Как называется услуга?", reply_markup=options([("Отмена", "admin:services")]))
    elif action_name == "shifts":
        await message.edit_text("Выберите мастера для настройки графика.", reply_markup=options([
            *[(master.name, f"admin:week:{master.id}") for master in await schedule.masters()],
            ("Назад", "admin:back"),
        ]))
    elif action_name == "week" and len(args) == 1 and args[0].isdigit():
        master_id = int(args[0])
        master = next((item for item in await schedule.masters() if item.id == master_id), None)
        if master is None:
            await message.edit_text("Мастер недоступен.", reply_markup=panel())
            return
        buttons = []
        for weekday, (start, end, breaks) in enumerate(await schedule.weekly_shifts(master_id)):
            label = "Выходной" if start is None or end is None else f"{time_label(start)}–{time_label(end)}"
            if breaks:
                label += f" · перерывов: {len(breaks)}"
            buttons.append((f"{WEEKDAYS[weekday]} · {label}", f"admin:shift:{master_id}:{weekday}"))
        await message.edit_text(f"График · {master.name}\nВыберите день недели.", reply_markup=options([
            *buttons, ("Разовый выходной", f"admin:exceptions:{master_id}"), ("Назад", "admin:shifts"),
        ]))
    elif action_name in {"exceptions", "close_date", "open_date"} and args and args[0].isdigit():
        master_id = int(args[0])
        dates = booking_days(moscow_now())
        if not any(item.id == master_id for item in await schedule.masters()):
            await message.edit_text("Мастер недоступен.", reply_markup=panel())
            return
        if action_name != "exceptions":
            day = next((item for item in dates if args[1:] == [item.isoformat()]), None)
            if day is None:
                await message.edit_text("Дата больше недоступна.", reply_markup=panel())
                return
            await schedule.set_date_off(master_id, day, action_name == "close_date")
        closed = await schedule.days_off(master_id)
        buttons = []
        for day in dates:
            label, operation = ("Снять выходной", "open_date") if day in closed else ("Назначить выходной", "close_date")
            buttons.append((f"{day:%d.%m} · {label}", f"admin:{operation}:{master_id}:{day}"))
        await message.edit_text("Разовые выходные. Уже оформленные визиты сохраняются.", reply_markup=options([
            *buttons, ("Назад", f"admin:week:{master_id}"),
        ]))
    elif action_name == "shift" and len(args) == 2 and all(arg.isdigit() for arg in args):
        master_id, weekday = map(int, args)
        if weekday >= len(WEEKDAYS) or not any(item.id == master_id for item in await schedule.masters()):
            await message.edit_text("День или мастер недоступен.", reply_markup=panel())
            return
        shift = (await schedule.weekly_shifts(master_id))[weekday]
        start, end, breaks = shift
        current = "Выходной" if start is None or end is None else f"{time_label(start)}–{time_label(end)}"
        if breaks:
            current += " | " + ", ".join(f"{time_label(lo)}–{time_label(hi)}" for lo, hi in breaks)
        await message.edit_text(f"{WEEKDAYS[weekday]} · {current}\n\n{SHIFT_PROMPT}", reply_markup=options([
            ("Выходной", f"admin:day_off:{master_id}:{weekday}"),
            ("Назад", f"admin:week:{master_id}"),
        ]))
        await state.update_data(shift_master=master_id, shift_weekday=weekday)
        await state.set_state(AdminForm.shift)
    elif action_name == "day_off" and len(args) == 2 and all(arg.isdigit() for arg in args):
        master_id, weekday = map(int, args)
        if weekday >= len(WEEKDAYS) or not any(item.id == master_id for item in await schedule.masters()):
            await message.edit_text("День или мастер недоступен.", reply_markup=panel())
            return
        await schedule.set_shift(master_id, weekday, (None, None, []))
        await message.edit_text(f"{WEEKDAYS[weekday]} · Выходной. Существующие записи сохранены.",
                                reply_markup=options([("Назад к графику", f"admin:week:{master_id}")]))
    elif action_name == "slots":
        await message.edit_text("Выберите мастера.", reply_markup=options([
            *[(master.name, f"admin:master:{master.id}") for master in await schedule.masters()],
            ("Назад", "admin:back"),
        ]))
    elif action_name == "master" and len(args) == 1 and args[0].isdigit():
        master_id = int(args[0])
        await message.edit_text("Выберите дату.", reply_markup=options([
            *[(day.strftime("%d.%m.%Y"), f"admin:day:{master_id}:{day.isoformat()}")
              for day in booking_days(moscow_now())],
            ("Назад", "admin:slots"),
        ]))
    elif action_name == "day" and len(args) == 2 and args[0].isdigit():
        try:
            day = date.fromisoformat(args[1])
        except ValueError:
            await message.edit_text("Выберите дату из календаря.")
            return
        master_id = int(args[0])
        master = next((master for master in await schedule.masters() if master.id == master_id), None)
        if master is None:
            await message.edit_text("Мастер недоступен.", reply_markup=panel())
            return
        slots, records, blocks = await schedule.day_schedule(day, master_id)
        shift = (await schedule.weekly_shifts(master_id))[day.weekday()]
        if day in await schedule.days_off(master_id):
            shift = (None, None, [])
        now = moscow_now()
        rows = {
            start: [
                InlineKeyboardButton(
                    text=f"{time_label(start)}–{time_label(start + duration)} · Занят",
                    callback_data="admin:occupied",
                ),
                InlineKeyboardButton(text="Отменить", callback_data=f"admin:ask:{booking_id}"),
            ]
            for start, duration, booking_id in records
            if day > now.date() or start + duration > minutes(now.time())
        }
        for slot in slots:
            start = minutes(slot.time)
            if any(overlaps(start, STEP_MINUTES, other, duration) for other, duration, _ in records):
                continue
            if slot.id not in blocks and not fits(start, STEP_MINUTES, [], {}, shift):
                continue
            label, operation = ("Разблокировать", "unblock") if slot.id in blocks else ("Заблокировать", "block")
            rows[start] = [InlineKeyboardButton(
                text=f"{time_label(start)} · {label}", callback_data=f"admin:{operation}:{master_id}:{slot.id}",
            )]
        await state.update_data(admin_return=f"admin:day:{master_id}:{day.isoformat()}")
        await message.edit_text(
            f"{master.name} · {day:%d.%m.%Y} (МСК)" if rows else "Слотов на эту дату нет.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                *[rows[start] for start in sorted(rows)],
                [InlineKeyboardButton(text="Назад", callback_data=f"admin:master:{master_id}")],
            ]),
        )
    elif action_name in {"block", "unblock"} and len(args) == 2 and all(arg.isdigit() for arg in args):
        master_id, slot_id = map(int, args)
        result = await schedule.set_blocked(master_id, slot_id, action_name == "block")
        await message.edit_text(result, reply_markup=options([("Назад", back)]))
    elif (action_name == "ask" and len(args) == 1 and args[0].isdigit()
          or action_name == "notice_ask" and len(args) == 2 and all(arg.isdigit() for arg in args)):
        item_id = int(args[0])
        revision = await booking.active_revision(item_id)
        from_notice = action_name == "notice_ask"
        if revision is None or (from_notice and revision != int(args[1])):
            if from_notice:
                await message.answer("Запись изменилась. Откройте её в разделе «Записи по датам».")
                return
            await message.edit_text("Запись уже недоступна.", reply_markup=options([("Назад", back)]))
            return
        if from_notice:
            back = "admin:notice_back"
        prompt = f"Отменить запись №{item_id}? Напишите причину для клиента или отмените без причины."
        markup = options([("Отменить без причины", f"admin:cancel:{item_id}"), ("Назад", back)])
        if from_notice:
            screen = await message.answer(prompt, reply_markup=markup)
            await state.update_data(screen_id=screen.message_id, admin_return=back)
        else:
            await message.edit_text(prompt, reply_markup=markup)
        await state.update_data(cancellation_id=item_id, cancellation_revision=revision, cancellation_reason=None)
        await state.set_state(AdminForm.cancellation_reason)
    else:
        await state.set_data({"screen_id": message.message_id})
        await message.edit_text("Панель администратора", reply_markup=panel())


@router.message(AdminForm.cancellation_reason, ~F.text.in_({BOOK, MY_BOOKINGS}))
async def enter_cancellation_reason(message: Message, state: FSMContext, admin_id: int) -> None:
    if message.from_user.id != admin_id:
        return
    data = await state.get_data()
    item_id = data["cancellation_id"]
    back = data.get("admin_return", "admin:back")
    reason = (message.text or "").strip()
    if not 1 <= len(reason) <= REASON_LIMIT:
        await show_screen(
            message, state, f"Введите причину текстом, до {REASON_LIMIT} символов.",
            options([("Отменить без причины", f"admin:cancel:{item_id}"), ("Назад", back)]),
            move_below=True,
        )
        return
    await show_screen(
        message, state, f"Запись №{item_id}\nПричина для клиента: {reason}",
        options([("Подтвердить отмену", f"admin:cancel:{item_id}"), ("Назад", back)]),
        move_below=True,
    )
    await state.update_data(cancellation_reason=reason)
    await state.set_state(AdminForm.cancellation_confirm)


@router.message(AdminForm.shift, ~F.text.in_({BOOK, MY_BOOKINGS}))
async def enter_shift(message: Message, state: FSMContext, admin_id: int, schedule: ScheduleService) -> None:
    if message.from_user.id != admin_id:
        return
    data = await state.get_data()
    master_id, weekday = data["shift_master"], data["shift_weekday"]
    markup = options([("Назад к графику", f"admin:week:{master_id}")])
    try:
        shift = parse_shift(message.text or "")
        await schedule.set_shift(master_id, weekday, shift)
    except ValueError:
        await show_screen(message, state, f"Проверьте смену и перерывы. Рабочие часы с 10:00 до 20:00.\n{SHIFT_PROMPT}",
                          markup, move_below=True)
        return
    await show_screen(message, state, "График обновлён. Существующие записи сохранены.", markup, move_below=True)
    await state.set_state(None)


@router.message(AdminForm.master_name, ~F.text.in_({BOOK, MY_BOOKINGS}))
async def enter_master_name(message: Message, state: FSMContext, admin_id: int) -> None:
    if message.from_user.id != admin_id:
        return
    name = normalize_name(message.text or "")
    back = ("Назад", "admin:masters")
    if name is None:
        await show_screen(message, state, "Введите имя мастера буквами.", options([back]), move_below=True)
        return
    await show_screen(message, state, f"Добавить мастера «{name}»?",
                      options([("Подтвердить добавление", "admin:save_master"), back]), move_below=True)
    await state.update_data(new_master_name=name)
    await state.set_state(AdminForm.master_confirm)


@router.message(AdminForm.service, ~F.text.in_({BOOK, MY_BOOKINGS}))
@router.message(AdminForm.price, ~F.text.in_({BOOK, MY_BOOKINGS}))
@router.message(AdminForm.duration, ~F.text.in_({BOOK, MY_BOOKINGS}))
async def add_service(message: Message, state: FSMContext, admin_id: int, booking: BookingService) -> None:
    if message.from_user.id != admin_id:
        return
    value = (message.text or "").strip()
    step = await state.get_state()
    markup = options([("Отмена", "admin:services")])
    if step == AdminForm.service.state:
        prompt = "Введите название от 1 до 60 символов."
        if 1 <= len(value) <= 60:
            await state.update_data(service_name=value)
            await state.set_state(AdminForm.price)
            prompt = "Сколько стоит услуга в рублях?"
    elif step == AdminForm.price.state:
        prompt = "Введите цену целым числом от 1 до 1 000 000."
        if value.isascii() and value.isdecimal() and len(value) <= 7 and 1 <= int(value) <= 1_000_000:
            await state.update_data(service_price=int(value))
            await state.set_state(AdminForm.duration)
            prompt = "Сколько минут длится услуга?"
    else:
        prompt = "Введите длительность от 1 до 600 минут."
        if value.isascii() and value.isdecimal() and len(value) <= 3 and 1 <= int(value) <= 600:
            data = await state.get_data()
            try:
                await booking.add_service(data["service_name"], data["service_price"], int(value))
            except IntegrityError:
                await state.set_state(AdminForm.service)
                prompt = "Это название уже занято. Введите другое название."
            else:
                await state.set_state(None)
                prompt = "Услуга добавлена."
                markup = options([("К услугам", "admin:services"), ("В админ-панель", "admin:back")])
    await show_screen(message, state, prompt, markup, move_below=True)
