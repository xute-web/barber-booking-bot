from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import FSInputFile, KeyboardButton, Message, ReplyKeyboardMarkup

from app.keyboards.menu import CANCEL, PORTFOLIO, main_menu
from app.keyboards.booking import options
from app.services.schedule_service import ScheduleService
from app.services.booking_service import BookingService

router = Router()


class ContactForm(StatesGroup):
    phone = State()


@router.message(F.text == PORTFOLIO)
async def portfolio(message: Message, state: FSMContext, schedule: ScheduleService) -> None:
    await state.clear()
    masters = await schedule.masters()
    if masters:
        await message.answer("Выберите мастера.", reply_markup=options([
            (master.name, f"styles:view:{master.id}") for master in masters
        ]))
    else:
        await message.answer("Сейчас нет доступных мастеров.")


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, admin_id: int) -> None:
    await state.clear()
    await message.answer_photo(FSInputFile(Path(__file__).resolve().parents[1] / "assets" / "welcome.png"),
                         caption="Добро пожаловать в BarberBot! Выберите действие.",
                         reply_markup=main_menu(is_admin=message.from_user.id == admin_id))


@router.message(F.text == CANCEL)
async def cancel_flow(message: Message, state: FSMContext, admin_id: int) -> None:
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_menu(is_admin=message.from_user.id == admin_id))


@router.message(Command("phone"))
async def request_phone(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(ContactForm.phone)
    await message.answer(
        "Поделитесь своим номером, чтобы получать в Telegram уведомления о записях по телефону.",
        reply_markup=ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="Поделиться своим номером", request_contact=True)],
            [KeyboardButton(text=CANCEL)],
        ], resize_keyboard=True, one_time_keyboard=True),
    )


@router.message(ContactForm.phone, F.contact)
async def link_phone(message: Message, state: FSMContext, booking: BookingService, admin_id: int) -> None:
    if message.contact.user_id != message.from_user.id:
        await message.answer("Отправьте свой номер кнопкой «Поделиться своим номером».")
        return
    try:
        await booking.link_contact(message.from_user.id, message.contact.phone_number)
    except ValueError:
        await message.answer("Для привязки нужен российский номер телефона.")
        return
    await state.clear()
    await message.answer("Номер привязан. Предстоящие записи по этому номеру доступны в «Моих записях».",
                         reply_markup=main_menu(is_admin=message.from_user.id == admin_id))
