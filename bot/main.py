"""Точка входа Telegram-бота."""

import asyncio
import logging

from aiogram import Bot, Dispatcher

from bot.handlers import admin, query, start
from bot.middlewares.limits import LimitsMiddleware
from config import settings

logger = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    admin.cleanup_addbook_tmp()

    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()

    dp.message.middleware(LimitsMiddleware())

    dp.include_router(admin.router)
    dp.include_router(start.router)
    dp.include_router(query.router)

    me = await bot.get_me()
    logger.info("Бот запущен: @%s", me.username)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
