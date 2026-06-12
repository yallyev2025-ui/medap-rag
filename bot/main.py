"""Точка входа Telegram-бота."""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from bot.handlers import admin, query, start
from bot.middlewares.limits import LimitsMiddleware
from config import settings

logger = logging.getLogger(__name__)

USER_COMMANDS = [
    BotCommand(command="start", description="Начать"),
    BotCommand(command="help", description="Как пользоваться"),
    BotCommand(command="limit", description="Сколько запросов осталось сегодня"),
]

ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand(command="stats", description="Статистика"),
    BotCommand(command="addbook", description="Добавить учебник"),
    BotCommand(command="broadcast", description="Рассылка всем пользователям"),
    BotCommand(command="ban", description="Заблокировать пользователя"),
    BotCommand(command="unban", description="Разблокировать пользователя"),
    BotCommand(command="premium", description="Выдать премиум"),
    BotCommand(command="unpremium", description="Убрать премиум"),
]


async def _setup_commands(bot: Bot) -> None:
    await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
    for admin_id in settings.ADMIN_IDS:
        try:
            await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception:
            logger.warning("Не удалось задать меню команд для админа %s", admin_id, exc_info=True)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    admin.cleanup_addbook_tmp()

    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()

    dp.message.middleware(LimitsMiddleware())

    dp.include_router(admin.router)
    dp.include_router(start.router)
    dp.include_router(query.router)

    await _setup_commands(bot)

    me = await bot.get_me()
    logger.info("Бот запущен: @%s", me.username)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
