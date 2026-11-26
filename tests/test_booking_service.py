import asyncio
from datetime import datetime, time, timedelta

import pytest
import pytest_asyncio

from app.db.database import create_database, initialize_database, session_factory
from app.services import booking_service, schedule_service
from app.services.booking_service import BookingService, BookingUnavailable, ClientBusy, ServiceUnavailable, SlotUnavailable
from app.services.schedule_service import ScheduleService, booking_days
from app.utils.validators import normalize_name, normalize_phone


@pytest_asyncio.fixture
async def app_services(tmp_path, monkeypatch):
    clock = [datetime(2026, 11, 26, 10)]
    monkeypatch.setattr(booking_service, "moscow_now", lambda: clock[0])
    monkeypatch.setattr(schedule_service, "moscow_now", lambda: clock[0])
    engine = create_database(tmp_path / "test.sqlite3")
    try:
        await initialize_database(engine)
        sessions = session_factory(engine)
        yield BookingService(sessions), ScheduleService(sessions), clock
    finally:
        await engine.dispose()


async def reserve(booking, slot, user=1, service=1, master=1):
    record, *_ = await booking.create(user, None, "Иван", "8 (900) 123-45-67", service, master, slot.id)
    return record


@pytest.mark.asyncio
async def test_calendar_and_default_services(app_services):
    booking, schedule, clock = app_services
    days = booking_days(clock[0])
    assert days == [clock[0].date() + timedelta(days=i) for i in range(7)]
    assert [slot.time for slot in await schedule.slots(days[0], 1, 60)][:2] == [time(10, 30), time(11)]
    assert len(await schedule.slots(days[-1], 1, 60)) == 16
    assert len(await schedule.slots(days[-1], 1, 30)) == 18
    assert await schedule.slots(days[-1] + timedelta(days=1), 1, 60) == []
    services = await booking.services()
    assert len(services) == 13
    assert [master.name for master in await schedule.masters()] == ["Сергей", "Максим", "Алексей"]
    assert [(service.name, service.price) for service in services[:3]] == [
        ("Стрижка", 1500), ("Борода", 800), ("Стрижка + борода", 2000),
    ]


@pytest.mark.asyncio
async def test_simultaneous_bookings_and_unavailable_service(app_services):
    booking, schedule, clock = app_services
    slot = (await schedule.slots(clock[0].date(), 1, 60))[0]
    with pytest.raises(ServiceUnavailable):
        await reserve(booking, slot, service=999)
    results = await asyncio.gather(
        reserve(booking, slot, user=1), reserve(booking, slot, user=2, service=3),
        return_exceptions=True,
    )
    assert sum(isinstance(result, SlotUnavailable) for result in results) == 1
    winner = next(result for result in results if not isinstance(result, Exception))
    assert len(await booking.for_client(winner.user_id)) == 1
    assert all(item.id != slot.id for item in await schedule.slots(clock[0].date(), 1, 60))


@pytest.mark.asyncio
async def test_cancellation_ownership_release_and_blocking(app_services):
    booking, schedule, clock = app_services
    slot = (await schedule.slots(clock[0].date(), 1, 60))[0]
    record = await reserve(booking, slot)
    assert await schedule.set_blocked(1, slot.id, True) == "Сначала отмените запись, затем заблокируйте слот"
    assert await booking.cancel(record.id, user_id=2) is None
    assert (await booking.cancel(record.id, user_id=1))[0].status == "cancelled"
    assert await booking.cancel(record.id, user_id=1) is None
    assert await booking.for_client(1) == []
    assert await schedule.set_blocked(1, slot.id, True) == "Слот заблокирован."
    with pytest.raises(SlotUnavailable):
        await reserve(booking, slot)
    assert await schedule.set_blocked(1, slot.id, False) == "Слот разблокирован."
    rebooked = await reserve(booking, slot)
    clock[0] = datetime.combine(slot.date, slot.time)
    assert await booking.for_client(1) == []
    assert await booking.cancel(rebooked.id, user_id=1) is None
    assert (await booking.cancel(rebooked.id))[0].status == "cancelled"


@pytest.mark.asyncio
async def test_reminder_boundary_and_short_notice(app_services):
    booking, schedule, clock = app_services
    tomorrow = clock[0].date() + timedelta(days=1)
    slots = await schedule.slots(tomorrow, 1, 60)
    exact = await reserve(booking, slots[0])
    clock[0] += timedelta(seconds=1)
    short = await reserve(booking, slots[1], user=2, master=2)
    # Rebook after the reminder cutoff for this start time.
    await booking.cancel(short.id)
    clock[0] += timedelta(minutes=30)
    await reserve(booking, slots[1], user=2, master=2)
    assert [record.id for record, *_ in await booking.due_reminders()] == [exact.id]
    await booking.mark_reminded(exact.id)
    assert await booking.due_reminders() == []


@pytest.mark.parametrize(("value", "expected"), [
    ("8 (900) 123-45-67", "+79001234567"),
    ("+7 900 123-45-67", "+79001234567"),
    ("79001234567", "+79001234567"),
    ("9001234567", None),
    ("+7 900 abc-45-67", None),
    ("+1 900 123-45-67", None),
    ("", None),
])
def test_phone_validation(value, expected):
    assert normalize_phone(value) == expected


@pytest.mark.parametrize(("value", "expected"), [
    (" Иван ", "Иван"),
    ("Анна-Мария", "Анна-Мария"),
    ("О’Коннор", "О’Коннор"),
    ("Иван  Петров", "Иван Петров"),
    ("Иван123", None),
    ("123", None),
    ("Иван🙂", None),
    ("Иван@Петров", None),
    ("Иван\nПетров", None),
    ("-Иван", None),
])
def test_name_validation(value, expected):
    assert normalize_name(value) == expected


@pytest.mark.asyncio
async def test_telegram_booking_and_admin_cancellation(app_services, monkeypatch):
    from itertools import count
    from unittest.mock import AsyncMock

    from aiogram import Bot
    from aiogram.methods import DeleteMessage, EditMessageText, SendMessage, SendPhoto
    from aiogram.types import InlineKeyboardMarkup, Message, ReplyKeyboardMarkup, Update

    from app.handlers import admin
    from app.main import build_dispatcher

    booking, schedule, clock = app_services
    monkeypatch.setattr(admin, "moscow_now", lambda: clock[0])
    client_id, admin_id = 100, 200
    dispatcher = build_dispatcher(booking, schedule, admin_id)
    updates = count(1)
    message_ids = count(1000)
    screen_id = None
    card_id = None
    admin_screen_id = None
    async with Bot(f"{300}:{'x' * 35}") as bot:
        async def telegram_request(bot, method, *, timeout=None):
            nonlocal screen_id, card_id, admin_screen_id
            if not isinstance(method, (SendMessage, SendPhoto)):
                return True
            message_id = next(message_ids)
            if method.chat_id == admin_id and isinstance(method.reply_markup, InlineKeyboardMarkup):
                admin_screen_id = message_id
            if method.chat_id == client_id and isinstance(method.reply_markup, InlineKeyboardMarkup):
                callback = method.reply_markup.inline_keyboard[0][0].callback_data
                if callback.startswith("book:"):
                    screen_id = message_id
                elif callback.startswith(("visit:move:", "mine:cancel:")):
                    card_id = message_id
            return Message.model_validate({
                "message_id": message_id, "date": 0,
                "chat": {"id": method.chat_id, "type": "private"}, "text": method.caption if isinstance(method, SendPhoto) else method.text,
            }, context={"bot": bot})

        send = AsyncMock(side_effect=telegram_request)
        monkeypatch.setattr(bot.session, "make_request", send)

        async def dispatch(user_id, *, message=None, callback=None, contact=None):
            user = {"id": user_id, "is_bot": False, "first_name": "Иван"}
            update_id = next(updates)
            message_id = update_id
            if callback == "book:manual":
                message_id = admin_screen_id
            elif callback and callback.startswith("book:"):
                message_id = admin_screen_id if user_id == admin_id else screen_id
            elif callback and callback.startswith(("mine:", "visit:")):
                message_id = card_id
            elif user_id == admin_id and callback and callback.startswith("admin:"):
                message_id = admin_screen_id
            telegram_message = {
                "message_id": message_id,
                "date": 1,
                "chat": {"id": user_id, "type": "private"}, "from": user,
                "text": message or "Меню",
            }
            if contact is not None:
                telegram_message.pop("text")
                telegram_message["contact"] = contact
            payload = {"update_id": update_id, "message": telegram_message}
            if callback is not None:
                payload = {"update_id": update_id, "callback_query": {
                    "id": str(update_id), "from": user, "chat_instance": "test",
                    "data": callback, "message": telegram_message,
                }}
            await dispatcher.feed_update(bot, Update.model_validate(payload, context={"bot": bot}))

        def messages():
            return [call.args[1] for call in send.await_args_list
                    if isinstance(call.args[1], SendMessage)]

        def edits():
            return [call.args[1] for call in send.await_args_list
                    if isinstance(call.args[1], EditMessageText)]

        def deleted_ids():
            return [call.args[1].message_id for call in send.await_args_list
                    if isinstance(call.args[1], DeleteMessage)]

        await dispatch(client_id, message="/start")
        welcome = send.await_args.args[1]
        assert isinstance(welcome, SendPhoto) and welcome.photo.path.is_file()
        assert isinstance(welcome.reply_markup, ReplyKeyboardMarkup)
        assert all(button.text != "Админ-панель" for row in welcome.reply_markup.keyboard for button in row)
        await dispatch(client_id, message="Записаться")
        await dispatch(client_id, callback="book:service:3")
        assert edits()[-1].text == "Выберите мастера."
        assert [button.callback_data for button in edits()[-1].reply_markup.inline_keyboard[-1]] == [
            "book:services", "book:cancel",
        ]
        await dispatch(client_id, callback="book:services")
        assert edits()[-1].text == "Выберите услугу."
        await dispatch(client_id, callback="book:service:3")
        await dispatch(client_id, callback="book:master:2")
        day = clock[0].date()
        await dispatch(client_id, callback=f"book:day:{day}")
        slot = (await schedule.slots(day, 2, 60))[0]
        await dispatch(client_id, callback=f"book:slot:{slot.id}")
        name_prompt_id = screen_id
        assert {edit.message_id for edit in edits()} == {name_prompt_id}
        await dispatch(client_id, message="Иван123")
        retry_name_id = screen_id
        assert messages()[-1].text.startswith("Введите имя буквами")
        await dispatch(client_id, message="Иван")
        phone_prompt_id = screen_id
        assert phone_prompt_id > name_prompt_id
        assert messages()[-1].text.startswith("Введите телефон")
        await dispatch(client_id, message="8 900 123-45-67")
        assert screen_id > phone_prompt_id
        assert deleted_ids() == [name_prompt_id, retry_name_id, phone_prompt_id]
        keyboard = messages()[-1].reply_markup
        assert isinstance(keyboard, InlineKeyboardMarkup)
        await dispatch(client_id, callback=keyboard.inline_keyboard[0][0].callback_data)
        notice = next(item for item in messages() if item.chat_id == admin_id and "Новая запись" in item.text)
        assert "Клиент: Иван" in notice.text and "Телефон: +79001234567" in notice.text
        assert notice.reply_markup.inline_keyboard[0][0].callback_data.startswith("admin:notice_ask:")
        assert "Вы записаны" in edits()[-1].text
        assert edits()[-1].reply_markup is None
        assert edits()[-1].message_id == screen_id
        assert len([item for item in messages() if item.chat_id == client_id]) == 4
        (record, _, _, master), = await booking.for_client(client_id)

        await dispatch(client_id, message="Записаться")
        await dispatch(client_id, callback="book:cancel")
        assert edits()[-1].message_id == screen_id
        assert edits()[-1].text == "Оформление записи отменено."

        await dispatch(client_id, callback=f"admin:cancel:{record.id}")
        assert len(await booking.for_client(client_id)) == 1
        assert master.name == "Максим"
        await dispatch(client_id, message="Мои записи")
        first_card_id = card_id
        total_messages = len(messages())
        await dispatch(client_id, message="Мои записи")
        assert len(messages()) == total_messages + 1
        assert card_id != first_card_id
        assert first_card_id in deleted_ids()
        await dispatch(client_id, callback=f"mine:ask:{record.id}")
        assert edits()[-1].message_id == card_id
        await dispatch(client_id, callback=f"mine:back:{record.id}")
        assert "Мастер: Максим" in edits()[-1].text
        await dispatch(client_id, callback=f"mine:ask:{record.id}")
        await dispatch(client_id, message="Не успеваю после работы.")
        assert "Причина отмены: Не успеваю после работы." == messages()[-1].text
        await dispatch(client_id, callback=f"mine:cancel:{record.id}")
        await dispatch(client_id, callback=f"mine:cancel:{record.id}")
        assert deleted_ids().count(card_id) == 1
        cancellations = [item for item in messages()
                         if item.chat_id == admin_id and "Клиент отменил" in item.text]
        assert len(cancellations) == 1
        assert "Максим" in cancellations[0].text
        assert "Причина: Не успеваю после работы." in cancellations[0].text
        assert await booking.for_client(client_id) == []
        record = await reserve(booking, slot, user=client_id, service=3, master=2)

        await dispatch(client_id, message="Админ-панель")
        assert messages()[-1].text == "Команда доступна только администратору."
        await dispatch(admin_id, message="/start")
        welcome = send.await_args.args[1]
        assert isinstance(welcome, SendPhoto) and welcome.photo.path.is_file()
        assert isinstance(welcome.reply_markup, ReplyKeyboardMarkup)
        assert welcome.reply_markup.keyboard[-1][0].text == "Админ-панель"
        await dispatch(admin_id, message="Админ-панель")
        assert messages()[-1].text == "Панель администратора"
        await dispatch(admin_id, callback="admin:slots")
        await dispatch(admin_id, callback="admin:master:2")
        await dispatch(admin_id, callback=f"admin:day:2:{day}")
        assert edits()[-1].message_id == admin_screen_id
        rows = edits()[-1].reply_markup.inline_keyboard
        occupied = [row for row in rows if any("Занят" in button.text for button in row)]
        assert [[button.text for button in row] for row in occupied] == [
            ["10:30–11:30 · Занят", "Отменить"],
        ]
        assert occupied[0][1].callback_data == f"admin:ask:{record.id}"
        assert not any(button.text.startswith("11:00") for row in rows for button in row)
        await dispatch(admin_id, callback=f"admin:block:2:{slot.id}")
        assert edits()[-1].text == "Сначала отмените запись, затем заблокируйте слот"
        await dispatch(admin_id, callback=f"admin:day:2:{day}")
        await dispatch(admin_id, callback=f"admin:ask:{record.id}")
        assert edits()[-1].message_id == admin_screen_id
        assert edits()[-1].reply_markup.inline_keyboard[-1][0].callback_data == f"admin:day:2:{day}"
        assert edits()[-1].reply_markup.inline_keyboard[0][0].text == "Отменить без причины"
        await dispatch(admin_id, message="  Мастер заболел.  ")
        assert "Причина для клиента: Мастер заболел." in messages()[-1].text
        assert len(await booking.for_client(client_id)) == 1
        await dispatch(admin_id, callback=f"admin:cancel:{record.id}")
        assert messages()[-1].chat_id == client_id
        assert "Причина: Мастер заболел." in messages()[-1].text
        assert await booking.for_client(client_id) == []
        assert slot.id in {item.id for item in await schedule.slots(day, 2, 60)}
        record = await reserve(booking, slot, user=client_id, service=3, master=2)
        await dispatch(admin_id, callback=f"admin:day:2:{day}")
        await dispatch(admin_id, callback=f"admin:ask:{record.id}")
        await dispatch(admin_id, callback=f"admin:cancel:{record.id}")
        assert "Администратор отменил" in messages()[-1].text
        assert "Причина:" not in messages()[-1].text
        assert await booking.for_client(client_id) == []
        await dispatch(admin_id, callback="admin:back")
        await dispatch(admin_id, callback="admin:add")
        previous_admin_screen = admin_screen_id
        await dispatch(admin_id, message="Уход")
        await dispatch(admin_id, message="900")
        await dispatch(admin_id, message="30")
        assert messages()[-1].text == "Услуга добавлена."
        assert admin_screen_id > previous_admin_screen
        assert previous_admin_screen in deleted_ids()
        service = next(s for s in await booking.services() if s.name == "Уход")
        await dispatch(admin_id, callback=f"admin:hide:{service.id}")
        assert service.id in [s.id for s in await booking.services()]
        await dispatch(admin_id, callback=f"admin:remove_service:{service.id}")
        assert service.id not in [s.id for s in await booking.services()]

        await dispatch(client_id, message="/phone")
        assert messages()[-1].reply_markup.keyboard[0][0].request_contact
        await dispatch(client_id, contact={"user_id": client_id, "phone_number": "89001234567",
                                           "first_name": "Иван"})
        assert "Номер привязан" in messages()[-1].text

        await dispatch(admin_id, callback="admin:shifts")
        await dispatch(admin_id, callback="admin:week:1")
        await dispatch(admin_id, callback=f"admin:shift:1:{day.weekday()}")
        await dispatch(admin_id, callback=f"admin:day_off:1:{day.weekday()}")
        assert "Выходной" in edits()[-1].text
        assert not await schedule.slots(day, 1, 60)
        await dispatch(admin_id, callback="admin:back")
        await dispatch(admin_id, callback="book:manual")
        await dispatch(admin_id, callback="book:service:3")
        await dispatch(admin_id, callback="book:master:0")
        await dispatch(admin_id, callback=f"book:day:{day}")
        choices = edits()[-1].reply_markup.inline_keyboard
        assert not any("Сергей" in row[0].text for row in choices)
        chosen = next(row[0].callback_data for row in choices if "Максим" in row[0].text)
        await dispatch(admin_id, callback=chosen)
        assert edits()[-1].text == "Как зовут клиента?"
        await dispatch(admin_id, message="Иван")
        await dispatch(admin_id, message="8 900 123-45-67")
        await dispatch(admin_id, callback=messages()[-1].reply_markup.inline_keyboard[0][0].callback_data)
        assert "Администратор записал вас" in messages()[-1].text
        assert "Клиент: Иван" in edits()[-1].text and "Телефон: +79001234567" in edits()[-1].text
        (record, _, old_slot, _), = await booking.for_client(client_id)
        await dispatch(client_id, message="Мои записи")
        await dispatch(client_id, callback=f"visit:confirm:{record.id}:0")
        assert (await booking.for_client(client_id))[0][0].visit_confirmed
        await dispatch(client_id, callback=f"visit:move:{record.id}:0")
        await dispatch(client_id, callback="book:master:3")
        await dispatch(client_id, callback=f"book:day:{day}")
        await dispatch(client_id, callback=f"book:slot:{old_slot.id}")
        confirm_move = edits()[-1].reply_markup.inline_keyboard[0][0].callback_data
        await dispatch(client_id, callback=confirm_move)
        (moved, _, _, new_master), = await booking.for_client(client_id)
        assert moved.revision == 1 and not moved.visit_confirmed and new_master.name == "Алексей"
        assert "Было:" in messages()[-1].text and "Стало:" in messages()[-1].text
        await dispatch(client_id, callback=f"visit:confirm:{record.id}:0")
        assert not (await booking.for_client(client_id))[0][0].visit_confirmed
        notice = messages()[-1]
        assert "Клиент: Иван" in notice.text and "Телефон: +79001234567" in notice.text
        await dispatch(admin_id, callback=notice.reply_markup.inline_keyboard[0][0].callback_data)
        assert "Напишите причину" in messages()[-1].text
        await dispatch(admin_id, message="Мастер заболел.")
        await dispatch(admin_id, callback=f"admin:cancel:{record.id}")
        assert messages()[-1].chat_id == client_id
        assert "Причина: Мастер заболел." in messages()[-1].text

        await dispatch(admin_id, message="Админ-панель")
        await dispatch(admin_id, callback="admin:visits")
        tomorrow = day + timedelta(days=1)
        assert any(button.callback_data == f"admin:visits_day:{tomorrow}:0"
                   for row in edits()[-1].reply_markup.inline_keyboard for button in row)
        tomorrow_slot = (await schedule.slots(tomorrow, 3, 60))[0]
        future_record = await reserve(booking, tomorrow_slot, user=client_id, master=3)
        await dispatch(admin_id, callback=f"admin:visits_day:{tomorrow}:0")
        assert f"Запись №{future_record.id}" in edits()[-1].text

        await dispatch(admin_id, callback="admin:masters")
        await dispatch(admin_id, callback="admin:new_master")
        await dispatch(admin_id, message="Андрей")
        assert not any(item.name == "Андрей" for item in await schedule.masters())
        await dispatch(admin_id, callback="admin:save_master")
        assert any(item.name == "Андрей" for item in await schedule.masters())
        await dispatch(admin_id, callback="admin:masters")
        await dispatch(admin_id, callback="admin:remove_master:3")
        assert any(item.id == 3 for item in await schedule.masters())
        await dispatch(admin_id, callback="admin:save_master")
        assert not any(item.id == 3 for item in await schedule.masters())
        assert not await schedule.slots(tomorrow, 3, 60)
        assert any(item[0].id == future_record.id for item in await booking.on_day(tomorrow))
        active_slot = (await schedule.slots(tomorrow, 2, 60))[0]
        with pytest.raises(SlotUnavailable):
            await reserve(booking, active_slot, user=1000, master=3)


@pytest.mark.asyncio
async def test_outbox_retries_after_service_restart(app_services):
    from unittest.mock import AsyncMock

    from aiogram.exceptions import TelegramNetworkError
    from aiogram.methods import SendMessage

    from app.services.notifications import flush_outbox

    booking, schedule, clock = app_services
    slot = (await schedule.slots(clock[0].date(), 1, 60))[0]
    record, *_ = await booking.create(10, None, "Иван", "+79001234567", 1, 1, slot.id, notify_admin=20)
    bot = AsyncMock()
    bot.send_message.side_effect = [
        TelegramNetworkError(method=SendMessage(chat_id=20, text="test"), message="temporary"),
        True,
    ]
    await flush_outbox(bot, booking)
    assert await booking.upcoming(10, record.id, 0)
    restarted = BookingService(booking.sessions)
    await flush_outbox(bot, restarted)
    assert bot.send_message.await_count == 1
    clock[0] += timedelta(seconds=31)
    await flush_outbox(bot, restarted)
    assert bot.send_message.await_count == 2
    assert "Телефон: +79001234567" in bot.send_message.await_args.args[1]
    await flush_outbox(bot, restarted)
    assert bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_client_cannot_book_overlap_at_different_masters(app_services):
    booking, schedule, clock = app_services
    day = clock[0].date()
    slot = (await schedule.slots(day, 1, 60))[0]
    outcomes = await asyncio.gather(
        reserve(booking, slot, user=10, master=1),
        reserve(booking, slot, user=10, master=2),
        return_exceptions=True,
    )
    assert sum(isinstance(item, ClientBusy) for item in outcomes) == 1
    (kept, _, _, _), = await booking.for_client(10)
    later = next(item for item in await schedule.slots(day, 3, 60) if item.time == time(12))
    second = await reserve(booking, later, user=10, master=3)
    free_master = 2 if kept.master_id == 1 else 1
    with pytest.raises(ClientBusy):
        await booking.reschedule(10, second.id, 0, free_master, slot.id)
    assert len(await booking.for_client(10)) == 2
    assert await booking.upcoming(10, second.id, 0)


@pytest.mark.asyncio
async def test_reminder_is_corrected_when_visit_moves_during_delivery(app_services):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.services.notifications import send_reminders

    booking, schedule, clock = app_services
    day = clock[0].date() + timedelta(days=1)
    first, second, *_ = await schedule.slots(day, 1, 60)
    record = await reserve(booking, first)
    bot = AsyncMock()

    async def move_during_delivery(*args, **kwargs):
        await booking.reschedule(1, record.id, 0, 1, second.id)
        return SimpleNamespace(message_id=99)

    bot.send_message.side_effect = move_during_delivery
    await send_reminders(bot, booking)
    assert bot.edit_message_text.await_args.kwargs["message_id"] == 99
    assert bot.edit_message_text.await_args.kwargs["reply_markup"] is None
    assert "устарело" in bot.edit_message_text.await_args.kwargs["text"]
    assert await booking.upcoming(1, record.id, 1)
    assert not await booking.confirm_visit(1, record.id, 0)


@pytest.mark.asyncio
async def test_upgrade_keeps_visits_and_can_edit_previously_sent_reminder(app_services):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.services.notifications import flush_outbox, send_reminders

    booking, schedule, clock = app_services
    day = clock[0].date() + timedelta(days=1)
    first, second, *_ = await schedule.slots(day, 1, 60)
    record = await reserve(booking, first)
    engine = booking.sessions.kw["bind"]
    async with engine.begin() as connection:
        await connection.exec_driver_sql("ALTER TABLE bookings DROP COLUMN reminder_message_id")
    await initialize_database(engine)
    assert await booking.upcoming(1, record.id, 0)

    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=101)
    await send_reminders(bot, booking)
    await booking.reschedule(1, record.id, 0, 1, second.id)
    await flush_outbox(bot, booking)
    assert bot.edit_message_text.await_args.kwargs["message_id"] == 101
    assert "10:30" in bot.edit_message_text.await_args.kwargs["text"]
    assert bot.edit_message_text.await_args.kwargs["reply_markup"] is None


@pytest.mark.asyncio
async def test_one_date_off_preserves_existing_visits(app_services):
    booking, schedule, clock = app_services
    day = clock[0].date() + timedelta(days=2)
    first, second, *_ = await schedule.slots(day, 1, 30)
    existing = await reserve(booking, first, service=4)
    await schedule.set_date_off(1, day, True)
    assert not await schedule.slots(day, 1, 30)
    with pytest.raises(SlotUnavailable):
        await reserve(booking, second, user=2, service=4)
    assert (await booking.on_day(day))[0][0].id == existing.id
    assert await schedule.slots(day, 2, 30)
    clock[0] += timedelta(days=7)
    assert await schedule.slots(day + timedelta(days=7), 1, 30)
    clock[0] -= timedelta(days=7)
    await schedule.set_date_off(1, day, False)
    assert second.id in {s.id for s in await schedule.slots(day, 1, 30)}


@pytest.mark.asyncio
async def test_shifts_preserve_visits_and_apply_to_new_bookings(app_services):
    booking, schedule, clock = app_services
    day = clock[0].date()
    slots = {slot.time: slot for slot in await schedule.slots(day, 1, 40)}
    existing = await reserve(booking, slots[time(13)], service=6)
    await schedule.set_shift(1, day.weekday(), (720, 1080, [[780, 840], [900, 930]]))
    available = {slot.time for slot in await schedule.slots(day, 1, 40)}
    assert time(12) in available
    assert not {time(11, 30), time(12, 30), time(13), time(15), time(17, 30)} & available
    assert (await booking.for_client(1))[0][0].id == existing.id
    with pytest.raises(SlotUnavailable):
        await reserve(booking, slots[time(15)], service=6, user=10)
    next_week = day + timedelta(days=7)
    assert (await schedule.weekly_shifts(1))[next_week.weekday()] == (720, 1080, [[780, 840], [900, 930]])


@pytest.mark.asyncio
async def test_reschedule_keeps_original_if_destination_is_taken(app_services):
    booking, schedule, clock = app_services
    slots = {slot.time: slot for slot in await schedule.slots(clock[0].date(), 1, 40)}
    record = await reserve(booking, slots[time(15)], user=10, service=6)
    await reserve(booking, slots[time(16)], user=20, service=6)
    with pytest.raises(SlotUnavailable):
        await booking.reschedule(10, record.id, 0, 1, slots[time(15, 30)].id)
    assert (await booking.for_client(10))[0][2].id == slots[time(15)].id
    with pytest.raises(BookingUnavailable):
        await booking.reschedule(20, record.id, 0, 3, slots[time(15, 30)].id)
    (moved, _, _, _), old_slot, _ = await booking.reschedule(10, record.id, 0, 3, slots[time(15, 30)].id)
    assert old_slot.id == slots[time(15)].id
    assert moved.revision == 1
    assert await booking.cancel(record.id, user_id=10, revision=0) is None
    assert time(15) in {slot.time for slot in await schedule.slots(clock[0].date(), 1, 40)}


@pytest.mark.asyncio
async def test_phone_link_and_reminder_buttons(app_services):
    from unittest.mock import AsyncMock

    from app.services.notifications import send_reminders

    booking, schedule, clock = app_services
    tomorrow = clock[0].date() + timedelta(days=1)
    slot = (await schedule.slots(tomorrow, 1, 60))[0]
    record = await reserve(booking, slot, user=None)
    assert record.user_id is None
    assert await booking.due_reminders() == []
    await booking.link_contact(100, "8 900 123-45-67")
    bot = AsyncMock()
    await send_reminders(bot, booking)
    assert bot.send_message.await_args.args[0] == 100
    buttons = bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
    assert [row[0].callback_data for row in buttons] == [
        f"visit:confirm:{record.id}:0", f"visit:move:{record.id}:0",
    ]
    assert await booking.due_reminders() == []


@pytest.mark.asyncio
async def test_migrate_existing_multi_master_database(tmp_path, monkeypatch):
    engine = create_database(tmp_path / "existing.sqlite3")
    monkeypatch.setattr(booking_service, "moscow_now", lambda: datetime(2026, 11, 26, 10))
    try:
        await initialize_database(engine)
        async with engine.begin() as connection:
            await connection.exec_driver_sql("ALTER TABLE masters DROP COLUMN is_active")
            await connection.exec_driver_sql("DROP TABLE bookings")
            await connection.exec_driver_sql(
                "CREATE TABLE bookings (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, username TEXT, "
                "full_name TEXT NOT NULL, phone TEXT NOT NULL, service_id INTEGER NOT NULL REFERENCES services(id), "
                "master_id INTEGER NOT NULL REFERENCES masters(id), "
                "time_slot_id INTEGER NOT NULL REFERENCES time_slots(id), duration_minutes INTEGER NOT NULL, "
                "status TEXT NOT NULL, created_at DATETIME NOT NULL, reminder_sent BOOLEAN NOT NULL)"
            )
            await connection.exec_driver_sql("CREATE INDEX ix_bookings_user_id ON bookings (user_id)")
            await connection.exec_driver_sql(
                "CREATE UNIQUE INDEX one_active_booking_per_slot ON bookings (master_id, time_slot_id) "
                "WHERE status = 'active'"
            )
            await connection.exec_driver_sql(
                "INSERT INTO time_slots VALUES (1, '2026-11-27', '13:00:00.000000')"
            )
            await connection.exec_driver_sql(
                "INSERT INTO bookings VALUES "
                "(1, 20, NULL, 'Иван', '+79001234567', 6, 2, 1, 40, 'active', '2026-11-26 10:00:00', 1)"
            )
        await initialize_database(engine)
        await initialize_database(engine)
        booking = BookingService(session_factory(engine))
        (record, service, slot, master), = await booking.for_client(20)
        assert (record.revision, record.visit_confirmed, record.reminder_sent) == (0, False, True)
        assert (record.duration_minutes, master.name, service.name) == (40, "Максим", "Детская стрижка")
        offline, *_ = await booking.create(None, None, "Пётр", "+79001112233", 6, 1, slot.id)
        assert offline.user_id is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_lunch_is_enforced_in_custom_shifts_and_during_booking(app_services):
    booking, schedule, clock = app_services
    day = clock[0].date()
    await schedule.set_shift(1, day.weekday(), (600, 1200, []))
    slots, _, _ = await schedule.day_schedule(day, 1)
    grid = {slot.time: slot for slot in slots}
    short = {slot.time for slot in await schedule.slots(day, 1, 15)}
    normal = {slot.time for slot in await schedule.slots(day, 1, 40)}
    assert {time(13, 30), time(15)} <= short
    assert not {time(14), time(14, 30)} & short
    assert time(13, 30) not in normal
    for start in (time(13, 30), time(14), time(14, 30)):
        with pytest.raises(SlotUnavailable):
            await reserve(booking, grid[start], service=6)


@pytest.mark.asyncio
async def test_overlapping_starts_and_independent_masters(app_services):
    booking, schedule, clock = app_services
    day = clock[0].date()
    slots = {slot.time: slot for slot in await schedule.slots(day, 1, 40)}
    first = await reserve(booking, slots[time(15)], service=6)
    assert first.duration_minutes == 40
    available = {slot.time for slot in await schedule.slots(day, 1, 40)}
    assert not {time(14, 30), time(15), time(15, 30), time(19, 30)} & available
    assert time(16) in available
    assert await schedule.set_blocked(1, slots[time(15, 30)].id, True) == (
        "Сначала отмените запись, затем заблокируйте слот"
    )
    second = await reserve(booking, slots[time(15)], user=2, service=6, master=2)
    assert second.master_id == 2
    assert await schedule.set_blocked(3, slots[time(15, 30)].id, True) == "Слот заблокирован."
    with pytest.raises(SlotUnavailable):
        await reserve(booking, slots[time(15)], user=3, service=6, master=3)
    await booking.cancel(first.id)
    assert time(15, 30) in {slot.time for slot in await schedule.slots(day, 1, 40)}
    assert time(15, 30) not in {slot.time for slot in await schedule.slots(day, 2, 40)}


@pytest.mark.asyncio
async def test_concurrent_overlapping_starts(app_services):
    booking, schedule, clock = app_services
    slots = {slot.time: slot for slot in await schedule.slots(clock[0].date(), 1, 40)}
    results = await asyncio.gather(
        reserve(booking, slots[time(15)], user=10, service=6),
        reserve(booking, slots[time(15, 30)], user=20, service=6),
        return_exceptions=True,
    )
    assert sum(isinstance(result, SlotUnavailable) for result in results) == 1


@pytest.mark.asyncio
async def test_migrate_existing_bookings_and_hourly_blocks(tmp_path, monkeypatch):
    now = datetime(2026, 11, 26, 10)
    monkeypatch.setattr(schedule_service, "moscow_now", lambda: now)
    engine = create_database(tmp_path / "legacy.sqlite3")
    try:
        async with engine.begin() as connection:
            for statement in [
                "CREATE TABLE services (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, "
                "price INTEGER NOT NULL, is_active BOOLEAN NOT NULL)",
                "CREATE TABLE time_slots (id INTEGER PRIMARY KEY, date DATE NOT NULL, time TIME NOT NULL, "
                "is_available BOOLEAN NOT NULL, is_blocked BOOLEAN NOT NULL, UNIQUE(date, time))",
                "CREATE TABLE bookings (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, username TEXT, "
                "full_name TEXT NOT NULL, phone TEXT NOT NULL, service_id INTEGER NOT NULL REFERENCES services(id), "
                "time_slot_id INTEGER NOT NULL REFERENCES time_slots(id), status TEXT NOT NULL, "
                "created_at DATETIME NOT NULL, reminder_sent BOOLEAN NOT NULL)",
                "CREATE INDEX ix_bookings_user_id ON bookings (user_id)",
                "CREATE UNIQUE INDEX one_active_booking_per_slot ON bookings (time_slot_id) WHERE status = 'active'",
                "INSERT INTO services VALUES (1, 'Стрижка', 1500, 1)",
                "INSERT INTO time_slots VALUES (1, '2026-11-27', '13:00:00.000000', 0, 0)",
                "INSERT INTO time_slots VALUES (2, '2026-11-27', '15:00:00.000000', 1, 1)",
                "INSERT INTO bookings VALUES "
                "(1, 1, NULL, 'Иван', '+79001234567', 1, 1, 'active', '2026-11-26 10:00:00', 1)",
            ]:
                await connection.exec_driver_sql(statement)
        await initialize_database(engine)
        await initialize_database(engine)
        sessions = session_factory(engine)
        booking, schedule = BookingService(sessions), ScheduleService(sessions)
        day = now.date() + timedelta(days=1)
        (record, service, _, master), = await booking.on_day(day)
        assert (record.id, record.reminder_sent, record.duration_minutes) == (1, True, 60)
        assert (service.name, service.duration_minutes, master.name) == ("Стрижка", 60, "Сергей")
        assert len(await booking.services()) == 13
        available = {slot.time for slot in await schedule.slots(day, 1, 30)}
        assert not {time(13), time(13, 30), time(15), time(15, 30)} & available
        assert {time(12, 30), time(16)} <= available
        assert {time(13), time(13, 30), time(15), time(15, 30)} <= {
            slot.time for slot in await schedule.slots(day, 2, 30)
        }
    finally:
        await engine.dispose()
