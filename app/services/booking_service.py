import asyncio
from datetime import date, datetime, timedelta

from sqlalchemy import Select, delete, or_, select, text, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    Booking, ClientBookingScreen, ClientContact, Master, OutgoingNotification, Service, TimeSlot,
)
from app.keyboards.booking import slot_label
from app.services.schedule_service import (
    active_intervals, blocked_intervals, booking_days, date_shift, fits, future_slot, minutes, moscow_now, overlaps,
)
from app.utils.validators import normalize_name, normalize_phone

REMINDER_LEAD = timedelta(hours=24)
BookingDetails = tuple[Booking, Service, TimeSlot, Master]


class SlotUnavailable(Exception):
    pass


class ServiceUnavailable(Exception):
    pass


class BookingUnavailable(Exception):
    pass


class ClientBusy(SlotUnavailable):
    pass


def queue_notification(session: AsyncSession, chat_id: int, message: str,
                       *, buttons: list[list[str]] | None = None, message_id: int | None = None) -> None:
    session.add(OutgoingNotification(chat_id=chat_id, message=message, message_id=message_id,
                                     buttons=buttons or [], next_attempt_at=moscow_now()))


def visit_details(service: Service, slot: TimeSlot, master: Master, duration: int) -> str:
    return f"{service.name}\nМастер: {master.name}\n{slot_label(slot, duration)}"


class BookingService:
    def __init__(self, sessions: async_sessionmaker) -> None:
        self.sessions = sessions
        self.delivery_lock = asyncio.Lock()

    async def screen_id(self, user_id: int) -> int | None:
        async with self.sessions() as session:
            return await session.scalar(select(ClientBookingScreen.message_id)
                                        .where(ClientBookingScreen.user_id == user_id))

    async def save_screen(self, user_id: int, message_id: int | None) -> None:
        async with self.sessions.begin() as session:
            if message_id is None:
                await session.execute(delete(ClientBookingScreen).where(ClientBookingScreen.user_id == user_id))
            else:
                await session.execute(insert(ClientBookingScreen).values(user_id=user_id, message_id=message_id)
                                      .on_conflict_do_update(index_elements=["user_id"],
                                                             set_={"message_id": message_id}))

    async def _check_client(self, session: AsyncSession, user_id: int | None, phone: str,
                            slot: TimeSlot, duration: int, exclude: int = 0) -> None:
        identity = Booking.phone == phone
        if user_id is not None:
            identity = or_(Booking.user_id == user_id, (Booking.user_id.is_(None) & identity))
        rows = await session.execute(select(TimeSlot.time, Booking.duration_minutes).join(Booking).where(
            Booking.status == "active", Booking.id != exclude, TimeSlot.date == slot.date, identity,
        ))
        if any(overlaps(minutes(slot.time), duration, minutes(start), length) for start, length in rows):
            raise ClientBusy

    async def services(self) -> list[Service]:
        async with self.sessions() as session:
            return list(await session.scalars(
                select(Service).where(Service.is_active.is_(True)).order_by(Service.id)
            ))

    async def hide_service(self, service_id: int) -> None:
        async with self.sessions.begin() as session:
            await session.execute(update(Service).where(Service.id == service_id).values(is_active=False))

    async def add_service(self, name: str, price: int, duration: int) -> None:
        async with self.sessions.begin() as session:
            session.add(Service(name=name, price=price, duration_minutes=duration))

    async def _destination(self, session: AsyncSession, master_id: int, slot_id: int,
                           duration: int, now: datetime,
                           exclude_booking: int | None = None) -> tuple[TimeSlot, Master]:
        slot = await session.get(TimeSlot, slot_id)
        master = await session.get(Master, master_id)
        if (slot is None or master is None or not master.is_active or slot.date not in booking_days(now)
                or datetime.combine(slot.date, slot.time) <= now):
            raise SlotUnavailable
        bookings = await active_intervals(session, slot.date, master_id)
        blocks = await blocked_intervals(session, slot.date, master_id)
        shift = await date_shift(session, master_id, slot.date)
        if not fits(minutes(slot.time), duration,
                    [item for item in bookings if item[2] != exclude_booking], blocks, shift):
            raise SlotUnavailable
        return slot, master

    async def link_contact(self, user_id: int, phone: str) -> None:
        phone = normalize_phone(phone)
        if phone is None:
            raise ValueError("Номер телефона недоступен.")
        async with self.sessions.begin() as session:
            await session.execute(delete(ClientContact).where(ClientContact.phone == phone,
                                                              ClientContact.user_id != user_id))
            await session.execute(insert(ClientContact).values(user_id=user_id, phone=phone)
                                  .on_conflict_do_update(index_elements=["user_id"], set_={"phone": phone}))
            upcoming = select(TimeSlot.id).where(future_slot(moscow_now()))
            await session.execute(update(Booking).where(
                Booking.user_id.is_(None), Booking.phone == phone,
                Booking.status == "active", Booking.time_slot_id.in_(upcoming),
            ).values(user_id=user_id))

    async def create(self, user_id: int | None, username: str | None, full_name: str,
                     phone: str, service_id: int, master_id: int, slot_id: int,
                     *, notify_admin: int | None = None) -> BookingDetails:
        """Check overlaps and reserve the interval under a single SQLite write lock."""
        full_name = normalize_name(full_name)
        phone = normalize_phone(phone)
        if phone is None or full_name is None:
            raise ValueError("Проверьте имя и номер телефона.")
        async with self.sessions.begin() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            now = moscow_now()
            service = await session.get(Service, service_id)
            if service is None or not service.is_active:
                raise ServiceUnavailable
            duration = service.duration_minutes
            slot, master = await self._destination(session, master_id, slot_id, duration, now)
            manual = user_id is None
            if manual:
                user_id = await session.scalar(select(ClientContact.user_id).where(ClientContact.phone == phone))
            await self._check_client(session, user_id, phone, slot, duration)
            booking = Booking(user_id=user_id, username=username, full_name=full_name,
                              phone=phone, service_id=service_id, master_id=master_id,
                              time_slot_id=slot_id, duration_minutes=duration, created_at=now)
            session.add(booking)
            await session.flush()
            details = visit_details(service, slot, master, duration)
            if manual:
                if user_id is not None:
                    queue_notification(session, user_id, f"Администратор записал вас на визит.\n{details}")
            elif notify_admin is not None:
                queue_notification(
                    session, notify_admin,
                    f"Новая запись №{booking.id}\n{details}\n\nКлиент: {full_name}\nТелефон: {phone}",
                    buttons=[["Отменить запись", f"admin:notice_ask:{booking.id}:{booking.revision}"]],
                )
            return booking, service, slot, master

    def _details(self) -> Select[BookingDetails]:
        return (select(Booking, Service, TimeSlot, Master).select_from(Booking)
                .join(Service).join(TimeSlot).join(Master))

    def _active(self) -> Select[BookingDetails]:
        return (self._details().where(Booking.status == "active")
                .order_by(TimeSlot.date, TimeSlot.time, Master.id))

    async def for_client(self, user_id: int) -> list[BookingDetails]:
        async with self.sessions() as session:
            return [tuple(row) for row in await session.execute(
                self._active().where(Booking.user_id == user_id, future_slot(moscow_now()))
            )]

    async def upcoming(self, user_id: int, booking_id: int, revision: int) -> BookingDetails | None:
        async with self.sessions() as session:
            row = (await session.execute(self._active().where(
                Booking.user_id == user_id, Booking.id == booking_id, Booking.revision == revision,
                future_slot(moscow_now()),
            ))).one_or_none()
            return tuple(row) if row is not None else None

    async def active_revision(self, booking_id: int) -> int | None:
        async with self.sessions() as session:
            return await session.scalar(select(Booking.revision).where(
                Booking.id == booking_id, Booking.status == "active",
            ))

    async def reschedule(self, user_id: int, booking_id: int, revision: int,
                         master_id: int, slot_id: int,
                         *, notify_admin: int | None = None) -> tuple[BookingDetails, TimeSlot, Master]:
        async with self.sessions.begin() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            now = moscow_now()
            row = (await session.execute(self._active().where(
                Booking.id == booking_id, Booking.user_id == user_id, Booking.revision == revision,
                future_slot(now),
            ))).one_or_none()
            if row is None:
                raise BookingUnavailable
            record, service, old_slot, old_master = row
            if old_slot.id == slot_id and old_master.id == master_id:
                raise SlotUnavailable
            slot, master = await self._destination(
                session, master_id, slot_id, record.duration_minutes, now, record.id,
            )
            await self._check_client(session, user_id, record.phone, slot, record.duration_minutes, record.id)
            record.master_id = master_id
            record.time_slot_id = slot_id
            record.revision += 1
            record.rescheduled_at = now
            record.visit_confirmed = False
            record.reminder_sent = False
            if record.reminder_message_id is not None:
                queue_notification(
                    session, user_id,
                    f"Визит перенесён.\n{visit_details(service, slot, master, record.duration_minutes)}\n"
                    "Управление записью в разделе «Мои записи».",
                    message_id=record.reminder_message_id,
                )
                record.reminder_message_id = None
            if notify_admin is not None:
                duration = record.duration_minutes
                queue_notification(
                    session, notify_admin,
                    f"Клиент перенёс запись №{record.id}\n{service.name}\n"
                    f"Клиент: {record.full_name}\nТелефон: {record.phone}\n"
                    f"Было: {old_master.name} · {slot_label(old_slot, duration)}\n"
                    f"Стало: {master.name} · {slot_label(slot, duration)}",
                    buttons=[["Отменить запись", f"admin:notice_ask:{record.id}:{record.revision}"]],
                )
            return (record, service, slot, master), old_slot, old_master

    async def confirm_visit(self, user_id: int, booking_id: int, revision: int) -> bool:
        async with self.sessions.begin() as session:
            upcoming = select(TimeSlot.id).where(future_slot(moscow_now()))
            result = await session.scalar(update(Booking).where(
                Booking.id == booking_id, Booking.user_id == user_id, Booking.revision == revision,
                Booking.status == "active", Booking.time_slot_id.in_(upcoming),
            ).values(visit_confirmed=True).returning(Booking.id))
            return result is not None

    async def on_day(self, day: date) -> list[BookingDetails]:
        async with self.sessions() as session:
            return [tuple(row) for row in await session.execute(
                self._active().where(TimeSlot.date == day)
            )]

    async def cancel(self, booking_id: int, *, user_id: int | None = None,
                     revision: int | None = None, notify_admin: int | None = None,
                     notify_client: bool = False, reason: str | None = None) -> BookingDetails | None:
        """Cancel once and return the details needed by the notification handler."""
        async with self.sessions.begin() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            query = update(Booking).where(Booking.id == booking_id, Booking.status == "active")
            if revision is not None:
                query = query.where(Booking.revision == revision)
            if user_id is not None:
                upcoming = select(TimeSlot.id).where(future_slot(moscow_now()))
                query = query.where(Booking.user_id == user_id, Booking.time_slot_id.in_(upcoming))
            result = await session.scalar(query.values(status="cancelled").returning(Booking.id))
            if result is None:
                return None
            row = (await session.execute(self._details().where(Booking.id == result))).one()
            record, service, slot, master = row
            details = visit_details(service, slot, master, record.duration_minutes)
            reason_text = f"\nПричина: {reason}" if reason else ""
            if notify_admin is not None:
                queue_notification(
                    session, notify_admin,
                    f"Клиент отменил запись №{record.id}\n{details}\n"
                    f"Клиент: {record.full_name}\nТелефон: {record.phone}{reason_text}",
                )
            if notify_client and record.user_id is not None:
                queue_notification(session, record.user_id,
                                   f"Администратор отменил вашу запись №{record.id}.\n{details}{reason_text}")
            if record.reminder_message_id is not None and record.user_id is not None:
                queue_notification(session, record.user_id, "Эта запись отменена.",
                                   message_id=record.reminder_message_id)
                record.reminder_message_id = None
            return tuple(row)

    async def due_reminders(self) -> list[BookingDetails]:
        now = moscow_now()
        async with self.sessions() as session:
            rows = await session.execute(self._active().where(
                Booking.reminder_sent.is_(False), Booking.user_id.is_not(None), future_slot(now),
                TimeSlot.date <= (now + REMINDER_LEAD).date(),
            ))
            return [
                (booking, service, slot, master) for booking, service, slot, master in rows
                if (booking.rescheduled_at or booking.created_at)
                <= datetime.combine(slot.date, slot.time) - REMINDER_LEAD <= now
            ]

    async def mark_reminded(self, booking_id: int, revision: int = 0, *,
                            chat_id: int | None = None, message_id: int | None = None) -> None:
        async with self.sessions.begin() as session:
            result = await session.scalar(update(Booking).where(
                Booking.id == booking_id, Booking.revision == revision, Booking.status == "active",
            ).values(reminder_sent=True, reminder_message_id=message_id).returning(Booking.id))
            if result is None and chat_id is not None and message_id is not None:
                queue_notification(session, chat_id, "Это напоминание устарело. Откройте «Мои записи».",
                                   message_id=message_id)
