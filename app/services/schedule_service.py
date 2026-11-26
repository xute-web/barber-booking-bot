from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, and_, delete, or_, select, text, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import BlockedSlot, Booking, Master, MasterDayOff, MasterShift, TimeSlot

MOSCOW = ZoneInfo("Europe/Moscow")
OPENING = time(10)
CLOSING = time(20)
STEP_MINUTES = 30
WINDOW_DAYS = 7
Shift = tuple[int | None, int | None, list[list[int]]]
DEFAULT_SHIFT = (600, 1200, [])
LUNCH_START = 14 * 60
LUNCH_END = 15 * 60


def moscow_now() -> datetime:
    """Return Moscow wall time for comparison with local slot fields."""
    return datetime.now(MOSCOW).replace(tzinfo=None)


def booking_days(now: datetime) -> list[date]:
    return [now.date() + timedelta(days=offset) for offset in range(WINDOW_DAYS)]


def future_slot(now: datetime) -> ColumnElement[bool]:
    return or_(TimeSlot.date > now.date(),
               and_(TimeSlot.date == now.date(), TimeSlot.time > now.time()))


def overlaps(start: int, duration: int, other_start: int, other_duration: int) -> bool:
    return start < other_start + other_duration and other_start < start + duration


def minutes(value: time) -> int:
    return value.hour * 60 + value.minute


async def active_intervals(session: AsyncSession, day: date, master_id: int) -> list[tuple[int, int, int]]:
    rows = await session.execute(
        select(TimeSlot.time, Booking.duration_minutes, Booking.id).join(Booking)
        .where(TimeSlot.date == day, Booking.master_id == master_id, Booking.status == "active")
    )
    return [(minutes(start), duration, booking_id) for start, duration, booking_id in rows]


async def blocked_intervals(session: AsyncSession, day: date, master_id: int) -> dict[int, int]:
    rows = await session.execute(
        select(TimeSlot.id, TimeSlot.time).join(BlockedSlot)
        .where(TimeSlot.date == day, BlockedSlot.master_id == master_id)
    )
    return {slot_id: minutes(start) for slot_id, start in rows}


async def shift_for(session: AsyncSession, master_id: int, weekday: int) -> Shift:
    shift = await session.get(MasterShift, (master_id, weekday))
    return (shift.starts_at, shift.ends_at, shift.breaks) if shift else DEFAULT_SHIFT


def fits(start: int, duration: int, bookings: list[tuple[int, int, int]], blocks: dict[int, int],
         shift: Shift = DEFAULT_SHIFT) -> bool:
    opening, closing, breaks = shift
    return (
        opening is not None and closing is not None
        and opening <= start < start + duration <= closing
        and not overlaps(start, duration, LUNCH_START, LUNCH_END - LUNCH_START)
        and not any(overlaps(start, duration, lo, hi - lo) for lo, hi in breaks)
        and not any(overlaps(start, duration, other, length) for other, length, _ in bookings)
        and not any(overlaps(start, duration, other, STEP_MINUTES) for other in blocks.values())
    )


async def date_shift(session: AsyncSession, master_id: int, day: date) -> Shift:
    if await session.get(MasterDayOff, (master_id, day)) is not None:
        return None, None, []
    return await shift_for(session, master_id, day.weekday())


class ScheduleService:
    def __init__(self, sessions: async_sessionmaker) -> None:
        self.sessions = sessions

    async def masters(self) -> list[Master]:
        async with self.sessions() as session:
            return list(await session.scalars(select(Master).where(Master.is_active.is_(True)).order_by(Master.id)))

    async def add_master(self, name: str) -> None:
        """Create a master or restore an earlier master with the exact same name."""
        async with self.sessions.begin() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            master = await session.scalar(select(Master).where(Master.name == name))
            if master is None:
                session.add(Master(name=name))
            elif master.is_active:
                raise ValueError("Мастер с таким именем уже есть.")
            else:
                master.is_active = True

    async def remove_master(self, master_id: int) -> bool:
        """Remove the master from new bookings without losing existing appointments."""
        async with self.sessions.begin() as session:
            result = await session.scalar(update(Master).where(
                Master.id == master_id, Master.is_active.is_(True),
            ).values(is_active=False).returning(Master.id))
            return result is not None

    async def weekly_shifts(self, master_id: int) -> list[Shift]:
        async with self.sessions() as session:
            return [await shift_for(session, master_id, day) for day in range(7)]

    async def days_off(self, master_id: int) -> set[date]:
        async with self.sessions() as session:
            return set(await session.scalars(select(MasterDayOff.day).where(
                MasterDayOff.master_id == master_id, MasterDayOff.day >= moscow_now().date())))

    async def set_date_off(self, master_id: int, day: date, closed: bool) -> None:
        async with self.sessions.begin() as session:
            if closed:
                await session.execute(insert(MasterDayOff).values(master_id=master_id, day=day)
                                      .on_conflict_do_nothing())
            else:
                await session.execute(delete(MasterDayOff).where(MasterDayOff.master_id == master_id,
                                                                 MasterDayOff.day == day))

    async def set_shift(self, master_id: int, weekday: int, shift: Shift) -> None:
        opening, closing, breaks = shift
        if not 0 <= weekday <= 6 or (
            shift != (None, None, []) and (
                opening is None or closing is None
                or not minutes(OPENING) <= opening < closing <= minutes(CLOSING)
                or any(not opening <= lo < hi <= closing for lo, hi in breaks)
                or any(first[1] > second[0] for first, second in zip(breaks, breaks[1:]))
            )
        ):
            raise ValueError("Проверьте время смены и перерывов.")
        async with self.sessions.begin() as session:
            values = dict(starts_at=opening, ends_at=closing, breaks=breaks)
            await session.execute(insert(MasterShift).values(master_id=master_id, weekday=weekday, **values)
                                  .on_conflict_do_update(index_elements=["master_id", "weekday"], set_=values))

    async def day_schedule(self, day: date, master_id: int) -> tuple[
        list[TimeSlot], list[tuple[int, int, int]], dict[int, int],
    ]:
        """Return future grid starts and all occupied intervals for the day."""
        now = moscow_now()
        if day not in booking_days(now):
            return [], [], {}
        starts = range(minutes(OPENING), minutes(CLOSING), STEP_MINUTES)
        rows = [{"date": day, "time": time(start // 60, start % 60)} for start in starts]
        async with self.sessions.begin() as session:
            await session.execute(insert(TimeSlot).values(rows)
                                  .on_conflict_do_nothing(index_elements=["date", "time"]))
            slots = list(await session.scalars(select(TimeSlot)
                         .where(TimeSlot.date == day, future_slot(now)).order_by(TimeSlot.time)))
            return (slots, await active_intervals(session, day, master_id),
                    await blocked_intervals(session, day, master_id))

    async def slots(self, day: date, master_id: int, duration: int,
                    *, exclude_booking: int | None = None) -> list[TimeSlot]:
        slots, bookings, blocks = await self.day_schedule(day, master_id)
        async with self.sessions() as session:
            master = await session.get(Master, master_id)
            if master is None or not master.is_active:
                return []
            shift = await date_shift(session, master_id, day)
        bookings = [item for item in bookings if item[2] != exclude_booking]
        return [slot for slot in slots if fits(minutes(slot.time), duration, bookings, blocks, shift)]

    async def choices(self, day: date, master_id: int, duration: int,
                      *, exclude_booking: int | None = None) -> list[tuple[TimeSlot, Master]]:
        choices = [
            (slot, master)
            for master in await self.masters() if not master_id or master.id == master_id
            for slot in await self.slots(day, master.id, duration, exclude_booking=exclude_booking)
        ]
        return sorted(choices, key=lambda item: (item[0].time, item[1].id))

    async def set_blocked(self, master_id: int, slot_id: int, blocked: bool) -> str:
        """Serialize block changes with booking writes for this interval."""
        async with self.sessions.begin() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            now = moscow_now()
            slot = await session.get(TimeSlot, slot_id)
            if (slot is None or slot.date not in booking_days(now)
                    or datetime.combine(slot.date, slot.time) <= now
                    or await session.get(Master, master_id) is None):
                return "Слот больше недоступен."
            key = (master_id, slot_id)
            if not blocked:
                record = await session.get(BlockedSlot, key)
                if record is not None:
                    await session.delete(record)
                return "Слот разблокирован."
            bookings = await active_intervals(session, slot.date, master_id)
            if any(overlaps(minutes(slot.time), STEP_MINUTES, start, duration)
                   for start, duration, _ in bookings):
                return "Сначала отмените запись, затем заблокируйте слот"
            await session.execute(insert(BlockedSlot).values(master_id=master_id, time_slot_id=slot_id)
                                  .on_conflict_do_nothing())
            return "Слот заблокирован."
