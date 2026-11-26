from pathlib import Path

from sqlalchemy import URL, event
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import (
    AsyncConnection, AsyncEngine, async_sessionmaker, create_async_engine,
)

from app.db.models import Base, Booking, Master, Service

DEFAULT_SERVICES = [
    ("Стрижка", 1500, 60),
    ("Борода", 800, 60),
    ("Стрижка + борода", 2000, 60),
    ("Стрижка машинкой", 900, 30),
    ("Фейд", 1700, 50),
    ("Детская стрижка", 1200, 40),
    ("Королевское бритьё", 1300, 40),
    ("Бритьё головы", 1100, 30),
    ("Камуфляж седины волос", 1400, 40),
    ("Камуфляж седины бороды", 1000, 25),
    ("Мытьё и укладка", 600, 20),
    ("Окантовка волос", 500, 15),
    ("Удаление волос воском", 500, 15),
]


def create_database(path: Path) -> AsyncEngine:
    engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(path)))

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(connection, _record) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


async def migrate_legacy_database(connection: AsyncConnection) -> None:
    """Assign existing appointments and hourly blocks to the first master."""
    columns = await connection.exec_driver_sql("PRAGMA table_info(bookings)")
    if "master_id" in {column[1] for column in columns}:
        return

    await connection.exec_driver_sql(
        "ALTER TABLE services ADD COLUMN duration_minutes INTEGER NOT NULL DEFAULT 60 "
        "CHECK (duration_minutes BETWEEN 1 AND 600)"
    )
    await connection.exec_driver_sql(
        "INSERT OR IGNORE INTO time_slots (date, time, is_available, is_blocked) "
        "SELECT date, time(time, '+30 minutes') || '.000000', 1, 0 "
        "FROM time_slots WHERE is_blocked = 1"
    )
    await connection.exec_driver_sql(
        "INSERT INTO blocked_slots (master_id, time_slot_id) "
        "SELECT 1, target.id FROM time_slots AS original JOIN time_slots AS target "
        "ON target.date = original.date AND (target.id = original.id OR "
        "target.time = time(original.time, '+30 minutes') || '.000000') "
        "WHERE original.is_blocked = 1"
    )
    await connection.exec_driver_sql("ALTER TABLE bookings RENAME TO bookings_legacy")
    await connection.exec_driver_sql("DROP INDEX one_active_booking_per_slot")
    await connection.exec_driver_sql("DROP INDEX ix_bookings_user_id")
    await connection.run_sync(Booking.__table__.create)
    await connection.exec_driver_sql(
        "INSERT INTO bookings (id, user_id, username, full_name, phone, service_id, master_id, "
        "time_slot_id, duration_minutes, status, created_at, reminder_sent) "
        "SELECT id, user_id, username, full_name, phone, service_id, 1, time_slot_id, 60, "
        "status, created_at, reminder_sent FROM bookings_legacy"
    )
    await connection.exec_driver_sql("DROP TABLE bookings_legacy")
    await connection.exec_driver_sql("ALTER TABLE time_slots DROP COLUMN is_available")
    await connection.exec_driver_sql("ALTER TABLE time_slots DROP COLUMN is_blocked")


async def migrate_visit_tracking(connection: AsyncConnection) -> None:
    columns = await connection.exec_driver_sql("PRAGMA table_info(bookings)")
    if "revision" in {column[1] for column in columns}:
        return
    await connection.exec_driver_sql("ALTER TABLE bookings RENAME TO bookings_previous")
    await connection.exec_driver_sql("DROP INDEX one_active_booking_per_slot")
    await connection.exec_driver_sql("DROP INDEX ix_bookings_user_id")
    await connection.run_sync(Booking.__table__.create)
    fields = ("id, user_id, username, full_name, phone, service_id, master_id, time_slot_id, "
              "duration_minutes, status, created_at, reminder_sent")
    await connection.exec_driver_sql(f"INSERT INTO bookings ({fields}) SELECT {fields} FROM bookings_previous")
    await connection.exec_driver_sql("DROP TABLE bookings_previous")


async def initialize_database(engine: AsyncEngine) -> None:
    """Initialize or migrate the database in a single SQLite transaction."""
    async with engine.begin() as connection:
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
        await connection.run_sync(Base.metadata.create_all)
        master_columns = await connection.exec_driver_sql("PRAGMA table_info(masters)")
        if "is_active" not in {column[1] for column in master_columns}:
            await connection.exec_driver_sql("ALTER TABLE masters ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 1")
        await connection.execute(insert(Master).values([
            {"id": 1, "name": "Сергей"}, {"id": 2, "name": "Максим"}, {"id": 3, "name": "Алексей"},
        ]).on_conflict_do_nothing(index_elements=[Master.id]))
        await migrate_legacy_database(connection)
        await migrate_visit_tracking(connection)
        booking_columns = await connection.exec_driver_sql("PRAGMA table_info(bookings)")
        if "reminder_message_id" not in {column[1] for column in booking_columns}:
            await connection.exec_driver_sql("ALTER TABLE bookings ADD COLUMN reminder_message_id INTEGER")
        await connection.execute(insert(Service).values([
            {"name": name, "price": price, "duration_minutes": duration}
            for name, price, duration in DEFAULT_SERVICES
        ]).on_conflict_do_nothing(index_elements=[Service.name]))


def session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)
