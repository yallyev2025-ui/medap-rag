"""Хендлеры /start, /help, /limit. Полноценный /limit — фаза 04."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

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

LIMIT_STUB_TEXT = "Лимиты будут добавлены в следующем обновлении."


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(WELCOME_TEXT)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("limit"))
async def cmd_limit(message: Message) -> None:
    await message.answer(LIMIT_STUB_TEXT)
