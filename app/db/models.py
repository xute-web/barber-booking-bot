from datetime import date, datetime, time

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Master(Base):
    __tablename__ = "masters"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("1"))


class MasterDayOff(Base):
    __tablename__ = "master_days_off"

    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id"), primary_key=True)
    day: Mapped[date] = mapped_column(primary_key=True)


class MasterShift(Base):
    __tablename__ = "master_shifts"

    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id"), primary_key=True)
    weekday: Mapped[int] = mapped_column(primary_key=True)
    starts_at: Mapped[int | None]
    ends_at: Mapped[int | None]
    breaks: Mapped[list[list[int]]] = mapped_column(JSON)


class ClientContact(Base):
    __tablename__ = "client_contacts"

    user_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    phone: Mapped[str] = mapped_column(unique=True)


class ClientBookingScreen(Base):
    __tablename__ = "client_booking_screens"

    user_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    message_id: Mapped[int]


class OutgoingNotification(Base):
    __tablename__ = "outgoing_notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int]
    message: Mapped[str]
    message_id: Mapped[int | None]
    buttons: Mapped[list[list[str]]] = mapped_column(JSON)
    next_attempt_at: Mapped[datetime] = mapped_column(index=True)
    attempts: Mapped[int] = mapped_column(default=0)


class Service(Base):
    __tablename__ = "services"
    __table_args__ = (CheckConstraint("duration_minutes BETWEEN 1 AND 600"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    price: Mapped[int]
    duration_minutes: Mapped[int]
    is_active: Mapped[bool] = mapped_column(default=True)


class TimeSlot(Base):
    __tablename__ = "time_slots"
    __table_args__ = (UniqueConstraint("date", "time"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date]
    time: Mapped[time]


class BlockedSlot(Base):
    __tablename__ = "blocked_slots"

    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id"), primary_key=True)
    time_slot_id: Mapped[int] = mapped_column(ForeignKey("time_slots.id"), primary_key=True)


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'cancelled')"),
        CheckConstraint("duration_minutes BETWEEN 1 AND 600"),
        Index("one_active_booking_per_slot", "master_id", "time_slot_id", unique=True,
              sqlite_where=text("status = 'active'")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(index=True)
    username: Mapped[str | None]
    full_name: Mapped[str]
    phone: Mapped[str]
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id"))
    time_slot_id: Mapped[int] = mapped_column(ForeignKey("time_slots.id"))
    duration_minutes: Mapped[int]
    status: Mapped[str] = mapped_column(default="active")
    created_at: Mapped[datetime]
    reminder_sent: Mapped[bool] = mapped_column(default=False)
    visit_confirmed: Mapped[bool] = mapped_column(default=False, server_default=text("0"))
    revision: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    rescheduled_at: Mapped[datetime | None]
    reminder_message_id: Mapped[int | None]
