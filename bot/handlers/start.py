"""Хендлеры /start, /help, /limit."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from config import settings
from db.crud import get_or_create_user, get_today_usage
from db.session import async_session

router = Router()

WELCOME_TEXT = """Привет! Я MedAP — бот-ассистент для медицинских студентов.

Просто напиши мне вопрос по теме из учебников — я найду
ответ строго по материалам и пришлю с указанием источника.

Доступные команды:
/help — как пользоваться
/limit — сколько запросов осталось сегодня"""

HELP_TEXT = """Я отвечаю на вопросы строго по материалам из учебников MedAP.

Доступные режимы:
- Вопрос: "Что такое инфаркт миокарда?"
- Конспект: "Сделай конспект по теме инфаркт миокарда"
- Объяснение простыми словами: "Объясни простыми словами, что такое инфаркт"

Команды:
/start — приветствие
/help — это сообщение
/limit — сколько запросов осталось сегодня"""

UNLIMITED_TEXT = "У тебя безлимитный доступ ✅"


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(WELCOME_TEXT)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("limit"))
async def cmd_limit(message: Message) -> None:
    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user)
        await session.commit()

        if user.is_premium or user.id in settings.ADMIN_IDS:
            await message.answer(UNLIMITED_TEXT)
            return

        used = await get_today_usage(session, user.id)

    await message.answer(f"Сегодня использовано: {used}/{settings.FREE_DAILY_LIMIT}")
