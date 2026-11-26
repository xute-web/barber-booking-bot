import logging
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import select

from app.db.models import OutgoingNotification
from app.keyboards.booking import options, slot_label
from app.services import booking_service
from app.services.booking_service import BookingService

logger = logging.getLogger(__name__)


async def flush_outbox(bot: Bot, booking: BookingService) -> None:
    """Retry durable notifications without holding a SQLite transaction during delivery."""
    async with booking.delivery_lock:
        async with booking.sessions() as session:
            pending = list(await session.scalars(select(OutgoingNotification).where(
                OutgoingNotification.next_attempt_at <= booking_service.moscow_now(),
            ).order_by(OutgoingNotification.id).limit(50)))
        for item in pending:
            delay = None
            markup = options(item.buttons) if item.buttons else None
            try:
                if item.message_id is None:
                    await bot.send_message(item.chat_id, item.message, reply_markup=markup)
                else:
                    await bot.edit_message_text(chat_id=item.chat_id, message_id=item.message_id,
                                                text=item.message, reply_markup=markup)
            except TelegramRetryAfter as error:
                delay = error.retry_after
            except (TelegramForbiddenError, TelegramBadRequest) as error:
                logger.warning("Уведомление невозможно доставить: %s", type(error).__name__)
            except TelegramAPIError as error:
                delay = min(3600, 30 * 2 ** min(item.attempts, 7))
                logger.warning("Уведомление будет отправлено повторно: %s", type(error).__name__)
            async with booking.sessions.begin() as session:
                if delay is None:
                    await session.delete(item)
                else:
                    item.attempts += 1
                    item.next_attempt_at = booking_service.moscow_now() + timedelta(seconds=delay)
                    session.add(item)


async def send_reminders(bot: Bot, booking: BookingService) -> None:
    for record, service, slot, master in await booking.due_reminders():
        if record.user_id is None or await booking.upcoming(record.user_id, record.id, record.revision) is None:
            continue
        visit = f"{record.id}:{record.revision}"
        markup = options([
            ("Подтвердить визит", f"visit:confirm:{visit}"),
            ("Перенести запись", f"visit:move:{visit}"),
        ])
        try:
            message = await bot.send_message(
                record.user_id,
                f"Напоминаем о записи на услугу «{service.name}». Мастер: {master.name}.\n"
                f"{slot_label(slot, record.duration_minutes)}", reply_markup=markup,
            )
        except TelegramForbiddenError:
            await booking.mark_reminded(record.id, record.revision)
        except TelegramAPIError as error:
            logger.warning("Не удалось отправить напоминание: %s", type(error).__name__)
        else:
            message_id = getattr(message, "message_id", None)
            await booking.mark_reminded(record.id, record.revision, chat_id=record.user_id,
                                        message_id=message_id if isinstance(message_id, int) else None)
    await flush_outbox(bot, booking)
