import asyncio
import logging
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import ErrorEvent, Message
from aiogram.utils.token import TokenValidationError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.exc import SQLAlchemyError

from app.config import Config, load_config
from app.db.database import create_database, initialize_database, session_factory
from app.handlers import admin, booking, my_bookings, start
from app.services.booking_service import BookingService
from app.services.notifications import send_reminders
from app.services.schedule_service import MOSCOW, ScheduleService

logger = logging.getLogger(__name__)


def build_dispatcher(booking_service: BookingService, schedule: ScheduleService, admin_id: int) -> Dispatcher:
    dispatcher = Dispatcher(events_isolation=SimpleEventIsolation(), booking=booking_service,
                            schedule=schedule, admin_id=admin_id)
    private = Router()
    private.message.filter(F.chat.type == "private", F.from_user)
    private.callback_query.filter(F.message.chat.type == "private")
    private.include_routers(start.router, admin.router, my_bookings.router, booking.router)
    dispatcher.include_router(private)
    dispatcher.errors.register(handle_error)
    return dispatcher


async def handle_error(event: ErrorEvent) -> bool:
    """Report recoverable errors without printing update contents."""
    error = event.exception
    logger.error("Ошибка обработки обновления: %s", type(error).__name__)
    message = event.update.message
    if event.update.callback_query:
        message = event.update.callback_query.message
    if isinstance(message, Message) and message.chat.type == "private":
        try:
            await message.answer("Не удалось выполнить действие. Проверьте «Мои записи» или попробуйте ещё раз.")
        except TelegramAPIError:
            logger.warning("Не удалось отправить сообщение об ошибке.")
    return True


async def run(config: Config) -> None:
    """Initialize the database, restore reminder checks and start polling."""
    engine = create_database(config.database_path)
    try:
        await initialize_database(engine)
        sessions = session_factory(engine)
        booking_service = BookingService(sessions)
        dispatcher = build_dispatcher(booking_service, ScheduleService(sessions), config.admin_id)
        async with Bot(config.token) as bot:
            reminder_lock = asyncio.Lock()

            async def check_reminders() -> None:
                async with reminder_lock:
                    try:
                        await send_reminders(bot, booking_service)
                    except SQLAlchemyError as error:
                        logger.warning("Не удалось проверить напоминания: %s", type(error).__name__)

            scheduler = AsyncIOScheduler(timezone=MOSCOW)
            scheduler.add_job(check_reminders, "interval", seconds=30,
                              next_run_time=datetime.now(MOSCOW), coalesce=True, max_instances=1)
            scheduler.start()
            try:
                await dispatcher.start_polling(bot, close_bot_session=False)
            finally:
                scheduler.pause()
                await asyncio.sleep(0)
                async with reminder_lock:
                    scheduler.shutdown(wait=False)
                    await asyncio.sleep(0)
    finally:
        await engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    try:
        config = load_config()
    except ValueError as error:
        raise SystemExit(str(error)) from None
    try:
        asyncio.run(run(config))
    except TokenValidationError:
        raise SystemExit("Проверьте формат BOT_TOKEN в файле .env.") from None
    except TelegramAPIError:
        raise SystemExit("Не удалось запустить бота. Проверьте токен и подключение к Telegram.") from None
    except SQLAlchemyError:
        raise SystemExit("Не удалось открыть базу SQLite. Проверьте путь и права доступа.") from None


if __name__ == "__main__":
    main()
