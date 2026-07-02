"""Хендлеры /start, /help, /limit."""

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import BotCommandScopeChat, Message

from bot.commands import ADMIN_COMMANDS
from bot.handlers.menu import send_main_menu
from config import settings
from db.crud import daily_limit_for, get_or_create_user, get_today_usage
from db.session import async_session

logger = logging.getLogger(__name__)

router = Router()

HELP_TEXT = """Я отвечаю строго по загруженным материалам MedAP: учебникам и клиническим рекомендациями.

Как пользоваться:
1. /start — выбрать режим (📚 Учебники или 📋 Клин. рекомендации) и предмет/категорию.
2. Задавайте вопросы — я ищу ответ в выбранной базе и указываю источник.
3. Сменить предмет или режим можно кнопками под сообщением или командой /start.

Если ответа в загруженных материалах нет — я честно предупреждаю об этом и, если могу, отвечаю из общих знаний с пометкой «не из материалов».

Форматы вопроса:
- Вопрос: "Что такое инфаркт миокарда?"
- Конспект: "Сделай конспект по теме инфаркт миокарда"
- Объяснение простыми словами: "Объясни простыми словами, что такое инфаркт"

Команды:
/start — выбор режима
/help — это сообщение
/limit — сколько запросов осталось сегодня

⚠️ Я даю информацию для справки, а не медицинское назначение. Решение и ответственность — на пользователе."""

UNLIMITED_TEXT = "У тебя безлимитный доступ ✅"


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    if message.from_user.id in settings.ADMIN_IDS:
        try:
            await message.bot.set_my_commands(
                ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=message.chat.id)
            )
        except Exception:
            logger.warning("Не удалось задать меню команд для админа %s", message.from_user.id, exc_info=True)

    await send_main_menu(message)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("limit"))
async def cmd_limit(message: Message) -> None:
    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user)
        await session.commit()

        limit = daily_limit_for(user)
        if limit is None:
            await message.answer(UNLIMITED_TEXT)
            return

        used = await get_today_usage(session, user.id)

    await message.answer(f"Сегодня использовано: {used}/{limit}")
